"""Backup and restore (Settings → Backup).

A backup is one .zip in the backups folder (paths.USER_BACKUP_DIR, mapped to the host in the
compose file) holding a copy of the database and a manifest. The password hash and sign-in
sessions are removed from the copy; notification tokens and Remote pairing tokens stay in, so
the file must be kept private.

Restoring lets the user tick groups. Each group says exactly what it replaces, and nothing
outside the ticked groups is touched:

- settings groups replace those settings key by key;
- notification destinations and AI instructions replace their whole list;
- the AI group replaces AI servers, their models, per-model settings and test results;
- machines and containers are matched by name and updated; ones added since the backup stay;
- history is matched to containers by machine and container name, and replaces the history.

A copy of the current state is always saved first ("before-restore"), so a restore can be
undone by restoring that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__, db, paths, settings

log = logging.getLogger("dockllama.backup")

KINDS = ("auto", "manual", "before-restore", "uploaded")
KEEP_BEFORE_RESTORE = 5
NAME_RE = re.compile(r"^dockllama-(auto|manual|before-restore|uploaded)-\d{8}-\d{6}(?:-\d+)?\.zip$")

# Settings never written by a restore: sign-in, and state that belongs to this install.
NEVER_RESTORED = ("auth.", "setup.complete", "summary.last_sent")

SETTING_GROUPS = {
    "checking": ("checks.", "fixing.", "quiet."),
    "notifications": ("notify.", "summary."),
    "general": ("general.", "data.", "backup."),
    "ai": ("ai.",),
}

GROUPS = {
    "checking": "Checking and fixing settings (how often, restart mode and limits, quiet hours, log lines)",
    "notifications": "Notifications (where messages go, reminders, daily summary)",
    "general": "Other settings (time zone, history retention, backup schedule)",
    "ai": "AI servers, models, model settings and test results",
    "prompts": "AI instructions (your prompts and which one is in use)",
    "machines": "Machines (other machines running DockLlama Remote)",
    "containers": "Containers (watched or not, notes for the AI, restart groups and permissions)",
    "history": "History (check results, events, CPU/memory history)",
}

# Container columns that describe the user's choices; the rest is runtime state.
CONTAINER_RUNTIME = {"id", "machine_id", "name", "first_seen", "last_seen", "gone", "last_log_ts",
                     "web_fails", "web_last_ok", "web_last_error"}


class BackupError(ValueError):
    pass


# --- files ---------------------------------------------------------------------------------


def folder() -> Path:
    paths.USER_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return paths.USER_BACKUP_DIR


def folder_is_mapped() -> bool | None:
    """True when the backups folder is a mount from the host (survives the container being
    removed). None when it can't be told (not running in Linux)."""
    try:
        target = str(paths.USER_BACKUP_DIR.resolve())
        with open("/proc/self/mountinfo") as f:
            return any(line.split()[4] == target for line in f)
    except OSError:
        return None


def _path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise BackupError("That isn't a DockLlama backup file name.")
    p = folder() / name
    if not p.exists():
        raise BackupError("That backup doesn't exist any more.")
    return p


def _new_name(kind: str) -> Path:
    try:  # name it in the user's time zone (the container's clock is usually UTC)
        with db.session() as conn:
            tz = ZoneInfo(settings.get(conn, "general.timezone"))
        stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
    except Exception:  # noqa: BLE001 - a name is never worth failing a backup over
        stamp = time.strftime("%Y%m%d-%H%M%S")
    p = folder() / f"dockllama-{kind}-{stamp}.zip"
    n = 2
    while p.exists():
        p = folder() / f"dockllama-{kind}-{stamp}-{n}.zip"
        n += 1
    return p


def list_files() -> list[dict]:
    out = []
    for p in folder().glob("dockllama-*.zip"):
        m = NAME_RE.match(p.name)
        if not m:
            continue
        st = p.stat()
        out.append({"name": p.name, "kind": m.group(1), "size": st.st_size, "created": st.st_mtime})
    return sorted(out, key=lambda f: f["created"], reverse=True)


def delete_file(name: str) -> None:
    _path(name).unlink()


def _prune(kind: str, keep: int) -> None:
    for f in [f for f in list_files() if f["kind"] == kind][keep:]:
        try:
            (folder() / f["name"]).unlink()
        except OSError:
            log.warning("Couldn't delete old backup %s", f["name"])


# --- making a backup -----------------------------------------------------------------------


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return {
        "settings": one("SELECT COUNT(*) FROM settings"),
        "notifications": one("SELECT COUNT(*) FROM notify_targets"),
        "ai": one("SELECT COUNT(*) FROM ai_servers"),
        "tests": one("SELECT COUNT(*) FROM model_tests WHERE status = 'done'"),
        "prompts": one("SELECT COUNT(*) FROM prompts"),
        "machines": one("SELECT COUNT(*) FROM machines"),
        "containers": one("SELECT COUNT(*) FROM containers"),
        "history": one("SELECT COUNT(*) FROM checks"),
    }


def make(kind: str = "manual") -> dict:
    """Write a backup now. Safe while DockLlama is running (SQLite's online backup)."""
    if kind not in KINDS:
        raise BackupError("Unknown kind of backup.")
    target = _new_name(kind)
    with tempfile.TemporaryDirectory(dir=folder()) as tmp:
        copy = Path(tmp) / "dockllama.db"
        with db.session() as src:
            dst = sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
        with db.session(copy) as c:
            secret = [k for k, s in settings.REGISTRY.items() if not s.exportable]
            c.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(secret))})", secret)
            c.execute("DELETE FROM sessions")
            c.execute("PRAGMA journal_mode=DELETE")
            c.execute("VACUUM")
            manifest = {"app": "dockllama", "version": __version__, "schema": db.current_version(c),
                        "created": time.time(), "counts": _counts(c)}
        partial = target.with_suffix(".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            z.write(copy, "dockllama.db")
        os.replace(partial, target)  # never leave a half-written backup with a real name
    if kind == "auto":
        with db.session() as conn:
            _prune("auto", settings.get(conn, "backup.keep"))
    elif kind == "before-restore":
        _prune("before-restore", KEEP_BEFORE_RESTORE)
    log.info("Backup written: %s", target.name)
    return {"name": target.name, "size": target.stat().st_size}


def save_upload(data: bytes) -> str:
    """Keep an uploaded backup in the backups folder (after checking it is one)."""
    target = _new_name("uploaded")
    target.write_bytes(data)
    try:
        read_manifest(target)
    except BackupError:
        target.unlink()
        raise
    return target.name


# --- reading a backup ----------------------------------------------------------------------


def read_manifest(p: Path) -> dict:
    try:
        with zipfile.ZipFile(p) as z:
            manifest = json.loads(z.read("manifest.json"))
            z.getinfo("dockllama.db")
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise BackupError("That file isn't a DockLlama backup.") from exc
    if manifest.get("app") != "dockllama":
        raise BackupError("That file isn't a DockLlama backup.")
    if manifest.get("schema", 0) > db.LATEST:
        raise BackupError("This backup was made by a newer DockLlama. Update DockLlama, then restore it.")
    return manifest


def contents(name: str) -> dict:
    p = _path(name)
    m = read_manifest(p)
    return {"name": name, "created": m["created"], "version": m.get("version"), "counts": m.get("counts", {}),
            "groups": GROUPS}


def _open_copy(p: Path, tmp: Path) -> Path:
    """Extract the backup's database and bring it up to this DockLlama's schema."""
    with zipfile.ZipFile(p) as z:
        with z.open("dockllama.db") as src, open(tmp / "backup.db", "wb") as dst:
            shutil.copyfileobj(src, dst)
    db.migrate(tmp / "backup.db", backup_dir=tmp / "migration-copies")
    return tmp / "backup.db"


# --- restoring -----------------------------------------------------------------------------


def _cols(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _copy_rows(conn: sqlite3.Connection, table: str, where: str = "", args: tuple = ()) -> int:
    cols = [c for c in _cols(conn, table, "bk") if c in set(_cols(conn, table))]
    names = ", ".join(cols)
    return conn.execute(f"INSERT INTO main.{table} ({names}) SELECT {names} FROM bk.{table} {where}", args).rowcount


def _restore_settings(conn, prefixes: tuple[str, ...]) -> int:
    n = 0
    for row in conn.execute("SELECT key, value FROM bk.settings").fetchall():
        key = row["key"]
        if key.startswith(NEVER_RESTORED) or not key.startswith(prefixes) or key not in settings.REGISTRY:
            continue
        if key == "ai.active_prompt":  # restored with the AI instructions, which it points at
            continue
        if not settings.REGISTRY[key].exportable:
            continue
        try:
            settings.set(conn, key, json.loads(row["value"]))
            n += 1
        except settings.SettingError:
            log.warning("Skipped setting %s from the backup: not valid any more", key)
    return n


def _machine_map(conn) -> dict[int, int]:
    """Backup machine id → this install's machine id, matched by kind/name/address."""
    current = [dict(r) for r in conn.execute("SELECT * FROM main.machines")]
    out = {}
    for b in conn.execute("SELECT * FROM bk.machines"):
        match = None
        if b["kind"] == "local":
            match = next((m for m in current if m["kind"] == "local"), None)
        else:
            match = (next((m for m in current if m["kind"] == "remote" and m["name"] == b["name"]), None)
                     or next((m for m in current if m["kind"] == "remote" and m["url"] == b["url"]), None))
        if match:
            out[b["id"]] = match["id"]
    return out


def _restore_machines(conn) -> int:
    n = 0
    mapping = _machine_map(conn)
    for b in conn.execute("SELECT * FROM bk.machines WHERE kind = 'remote'").fetchall():
        if b["id"] in mapping:
            conn.execute("UPDATE main.machines SET name = ?, url = ?, token = ?, allows_restart = ?, enabled = ? "
                         "WHERE id = ?", (b["name"], b["url"], b["token"], b["allows_restart"], b["enabled"],
                                          mapping[b["id"]]))
        else:
            conn.execute("INSERT INTO main.machines (name, kind, url, token, allows_restart, enabled) "
                         "VALUES (?, 'remote', ?, ?, ?, ?)", (b["name"], b["url"], b["token"], b["allows_restart"],
                                                             b["enabled"]))
        n += 1
    return n


def _container_map(conn) -> dict[int, int]:
    machines_map = _machine_map(conn)
    current = {(r["machine_id"], r["name"]): r["id"] for r in conn.execute("SELECT id, machine_id, name FROM main.containers")}
    out = {}
    for b in conn.execute("SELECT id, machine_id, name FROM bk.containers"):
        mid = machines_map.get(b["machine_id"])
        if mid is not None and (mid, b["name"]) in current:
            out[b["id"]] = current[(mid, b["name"])]
    return out


def _restore_containers(conn) -> tuple[int, int]:
    machines_map = _machine_map(conn)
    prefs = [c for c in _cols(conn, "containers", "bk") if c in set(_cols(conn, "containers")) and c not in CONTAINER_RUNTIME]
    current = {(r["machine_id"], r["name"]): r["id"] for r in conn.execute("SELECT id, machine_id, name FROM main.containers")}
    done = skipped = 0
    now = time.time()
    for b in conn.execute("SELECT * FROM bk.containers").fetchall():
        mid = machines_map.get(b["machine_id"])
        if mid is None:
            skipped += 1  # its machine isn't here (restore Machines too)
            continue
        values = [b[c] for c in prefs]
        if (mid, b["name"]) in current:
            conn.execute(f"UPDATE main.containers SET {', '.join(c + ' = ?' for c in prefs)} WHERE id = ?",
                         (*values, current[(mid, b["name"])]))
        else:
            # Not seen on this install yet: kept, so its settings apply when it shows up.
            conn.execute(f"INSERT INTO main.containers (machine_id, name, first_seen, last_seen, {', '.join(prefs)}) "
                         f"VALUES (?, ?, ?, ?, {', '.join('?' * len(prefs))})", (mid, b["name"], now, now, *values))
        done += 1
    return done, skipped


def _restore_history(conn) -> int:
    cmap = _container_map(conn)
    conn.execute("DELETE FROM main.events")
    conn.execute("DELETE FROM main.stats")
    conn.execute("DELETE FROM main.container_state")
    conn.execute("DELETE FROM main.checks")
    check_cols = [c for c in _cols(conn, "checks", "bk") if c in set(_cols(conn, "checks")) and c not in ("id", "container_id")]
    check_ids: dict[int, int] = {}
    n = 0
    for r in conn.execute("SELECT * FROM bk.checks ORDER BY id").fetchall():
        if r["container_id"] not in cmap:
            continue
        check_ids[r["id"]] = conn.execute(
            f"INSERT INTO main.checks (container_id, {', '.join(check_cols)}) VALUES (?, {', '.join('?' * len(check_cols))})",
            (cmap[r["container_id"]], *[r[c] for c in check_cols])).lastrowid
        n += 1
    state_cols = [c for c in _cols(conn, "container_state", "bk") if c in set(_cols(conn, "container_state"))
                  and c not in ("container_id", "last_check_id")]
    for r in conn.execute("SELECT * FROM bk.container_state").fetchall():
        if r["container_id"] in cmap:
            conn.execute(f"INSERT INTO main.container_state (container_id, last_check_id, {', '.join(state_cols)}) "
                         f"VALUES (?, ?, {', '.join('?' * len(state_cols))})",
                         (cmap[r["container_id"]], check_ids.get(r["last_check_id"]), *[r[c] for c in state_cols]))
    for r in conn.execute("SELECT * FROM bk.stats").fetchall():
        if r["container_id"] in cmap:
            conn.execute("INSERT INTO main.stats (container_id, at, cpu, mem) VALUES (?, ?, ?, ?)",
                         (cmap[r["container_id"]], r["at"], r["cpu"], r["mem"]))
    for r in conn.execute("SELECT * FROM bk.events").fetchall():
        # handled = 1: a restore never re-sends old messages
        conn.execute("INSERT INTO main.events (at, container_id, kind, data, handled) VALUES (?, ?, ?, ?, 1)",
                     (r["at"], cmap.get(r["container_id"]), r["kind"], r["data"]))
    return n


def _restore_prompts(conn) -> int:
    conn.execute("DELETE FROM main.prompts")  # their test results go with them
    n = _copy_rows(conn, "prompts")
    row = conn.execute("SELECT value FROM bk.settings WHERE key = 'ai.active_prompt'").fetchone()
    active = json.loads(row["value"]) if row else 0
    if active and conn.execute("SELECT 1 FROM main.prompts WHERE id = ?", (active,)).fetchone() is None:
        active = 0
    settings.set(conn, "ai.active_prompt", active)
    return n


def _restore_ai(conn, with_prompt_tests: bool) -> int:
    conn.execute("DELETE FROM main.ai_servers")  # models, model settings and test results go with them
    n = _copy_rows(conn, "ai_servers")
    _copy_rows(conn, "models")
    _copy_rows(conn, "model_settings")
    keep = "" if with_prompt_tests else "WHERE prompt_id IS NULL"
    _copy_rows(conn, "test_runs", keep)
    _copy_rows(conn, "model_tests", keep)
    # A run that was in progress when the backup was made can't continue here.
    conn.execute("UPDATE main.test_runs SET status = 'cancelled', note = 'Restored from a backup.' "
                 "WHERE status IN ('waiting', 'scheduled', 'running', 'paused')")
    conn.execute("UPDATE main.model_tests SET status = 'cancelled' WHERE status IN ('queued', 'running')")
    if conn.execute("SELECT COUNT(*) FROM main.ai_servers WHERE connected = 1").fetchone()[0] > 1:
        conn.execute("UPDATE main.ai_servers SET connected = 0 WHERE id != (SELECT MAX(id) FROM main.ai_servers "
                     "WHERE connected = 1)")
    return n


def restore(name: str, groups: list[str]) -> dict:
    """Restore the ticked groups from a backup. Saves the current state first."""
    groups = [g for g in dict.fromkeys(groups) if g in GROUPS]
    if not groups:
        raise BackupError("Tick at least one thing to restore.")
    p = _path(name)
    read_manifest(p)
    safety = make("before-restore")["name"]
    done: dict[str, object] = {}
    with tempfile.TemporaryDirectory(dir=folder()) as tmp:
        copy = _open_copy(p, Path(tmp))
        with db.session() as conn:
            conn.execute("ATTACH DATABASE ? AS bk", (str(copy),))
            try:
                with db.transaction(conn):
                    for g, prefixes in SETTING_GROUPS.items():
                        if g in groups:
                            done[g + "_settings"] = _restore_settings(conn, prefixes)
                    if "notifications" in groups:
                        conn.execute("DELETE FROM main.notify_targets")
                        done["notifications"] = _copy_rows(conn, "notify_targets")
                    if "prompts" in groups:
                        done["prompts"] = _restore_prompts(conn)
                    if "ai" in groups:
                        done["ai"] = _restore_ai(conn, with_prompt_tests="prompts" in groups)
                    if "machines" in groups:
                        done["machines"] = _restore_machines(conn)
                    if "containers" in groups:
                        done["containers"], done["containers_skipped"] = _restore_containers(conn)
                    if "history" in groups:
                        done["history"] = _restore_history(conn)
                    problems = conn.execute("PRAGMA main.foreign_key_check").fetchall()
                    if problems:
                        raise BackupError("The backup didn't fit this install, so nothing was restored. "
                                          f"({len(problems)} links between records didn't match.)")
            finally:
                conn.execute("DETACH DATABASE bk")
    log.info("Restored %s from %s (current state saved as %s)", groups, name, safety)
    return {"restored": groups, "details": done, "saved_first": safety}


# --- automatic backups ---------------------------------------------------------------------


class AutoBackup:
    """Makes an automatic backup every `backup.every_hours` (default daily), keeping `backup.keep`."""

    CHECK_EVERY = 300

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="auto-backup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    @staticmethod
    def due() -> bool:
        with db.session() as conn:
            if not settings.get(conn, "backup.auto") or not settings.get(conn, "setup.complete"):
                return False
            every = settings.get(conn, "backup.every_hours") * 3600
        last = max((f["created"] for f in list_files() if f["kind"] == "auto"), default=0)
        return time.time() - last >= every

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # let startup finish first
        while True:
            try:
                if await asyncio.to_thread(self.due):
                    await asyncio.to_thread(make, "auto")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let backups take the app down
                log.exception("Automatic backup failed; will try again later")
            await asyncio.sleep(self.CHECK_EVERY)


AUTO = AutoBackup()
