"""Backup and restore: what's in a backup, what each restore group replaces, and what it never touches."""

import io
import json
import time
import zipfile

from dockllama import backup, db, paths, settings

from .test_model_tests import c, fake, wait_idle  # noqa: F401  (fixtures)


def _container(conn, name, machine_id=1, **prefs):
    now = time.time()
    cid = conn.execute("INSERT INTO containers (machine_id, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                       (machine_id, name, now, now)).lastrowid
    for k, v in prefs.items():
        conn.execute(f"UPDATE containers SET {k} = ? WHERE id = ?", (v, cid))
    return cid


def _check(conn, cid, status="healthy", summary="fine"):
    return conn.execute("INSERT INTO checks (container_id, at, status, source, summary) VALUES (?, ?, ?, 'ai', ?)",
                        (cid, time.time(), status, summary)).lastrowid


def test_backup_file_has_no_password_or_sessions(c):
    b = c.post("/api/backup/now").json()
    files = c.get("/api/backup").json()["files"]
    assert [f["name"] for f in files] == [b["name"]] and files[0]["kind"] == "manual"
    with zipfile.ZipFile(paths.USER_BACKUP_DIR / b["name"]) as z:
        manifest = json.loads(z.read("manifest.json"))
        z.extract("dockllama.db", paths.DATA_DIR / "x")
    assert manifest["schema"] == db.LATEST and manifest["counts"]["ai"] == 1
    with db.session(paths.DATA_DIR / "x" / "dockllama.db") as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'auth.password_hash'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 3  # everything else is there
    # the live install still has its password and sessions
    with db.session() as conn:
        assert settings.get(conn, "auth.password_hash")


def test_restore_only_what_was_ticked(c):
    with db.session() as conn, db.transaction(conn):
        web = _container(conn, "web", monitored=1, notes_for_ai="Brief reconnects are normal.")
        _check(conn, web, "degraded", "old verdict")
        settings.set(conn, "checks.interval_minutes", 15)
        settings.set(conn, "fixing.mode", "auto")
        conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) "
                     "VALUES ('ntfy', '{}', 'ntfys://ntfy.sh/a', '[]', 0)")
    name = c.post("/api/backup/now").json()["name"]

    # things change after the backup
    with db.session() as conn, db.transaction(conn):
        conn.execute("UPDATE containers SET notes_for_ai = 'changed', monitored = 0 WHERE name = 'web'")
        _container(conn, "newer", monitored=1)
        settings.set(conn, "checks.interval_minutes", 30)
        settings.set(conn, "fixing.mode", "watch")
        conn.execute("DELETE FROM notify_targets")
        conn.execute("DELETE FROM checks")
    c.put("/api/ai/model", json={"name": "gemma4:latest"})

    r = c.post(f"/api/backup/files/{name}/restore", json={"groups": ["containers", "checking"]}).json()
    assert r["restored"] == ["containers", "checking"] and r["saved_first"].startswith("dockllama-before-restore-")
    with db.session() as conn:
        web_row = conn.execute("SELECT * FROM containers WHERE name = 'web'").fetchone()
        assert web_row["notes_for_ai"] == "Brief reconnects are normal." and web_row["monitored"] == 1
        assert conn.execute("SELECT 1 FROM containers WHERE name = 'newer'").fetchone()  # added since: kept
        assert settings.get(conn, "checks.interval_minutes") == 15 and settings.get(conn, "fixing.mode") == "auto"
        # not ticked: untouched
        assert conn.execute("SELECT COUNT(*) FROM notify_targets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0
        assert conn.execute("SELECT model FROM ai_servers WHERE connected = 1").fetchone()["model"] == "gemma4:latest"
        # sign-in is never restored
        assert settings.get(conn, "auth.password_hash")


def test_restore_everything_and_undo(c):
    with db.session() as conn, db.transaction(conn):
        web = _container(conn, "web", monitored=1)
        chk = _check(conn, web, "unhealthy", "db down")
        conn.execute("INSERT INTO container_state (container_id, status, since, last_check_id) VALUES (?, 'unhealthy', 0, ?)",
                     (web, chk))
        conn.execute("INSERT INTO events (at, container_id, kind, handled) VALUES (?, ?, 'status_change', 0)",
                     (time.time(), web))
        conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) "
                     "VALUES ('ntfy', '{}', 'ntfys://ntfy.sh/a', '[]', 0)")
    pid = c.post("/api/prompts", json={"name": "mine"}).json()["id"]
    c.put("/api/prompts/active", json={"id": pid})
    name = c.post("/api/backup/now").json()["name"]

    # wipe a lot
    with db.session() as conn, db.transaction(conn):
        conn.execute("DELETE FROM containers")
        conn.execute("DELETE FROM notify_targets")
        conn.execute("DELETE FROM prompts")
        conn.execute("DELETE FROM ai_servers")
        settings.set(conn, "ai.active_prompt", 0)
        _container(conn, "web")  # the container exists again with a new id (e.g. fresh install)

    groups = list(backup.GROUPS)
    r = c.post(f"/api/backup/files/{name}/restore", json={"groups": groups})
    assert r.status_code == 200, r.text
    with db.session() as conn:
        web = conn.execute("SELECT * FROM containers WHERE name = 'web'").fetchone()
        assert web["monitored"] == 1
        st = conn.execute("SELECT * FROM container_state WHERE container_id = ?", (web["id"],)).fetchone()
        assert st["status"] == "unhealthy"
        k = conn.execute("SELECT * FROM checks WHERE id = ?", (st["last_check_id"],)).fetchone()
        assert k["summary"] == "db down" and k["container_id"] == web["id"]  # history follows the container by name
        # restored events never re-send old messages
        assert conn.execute("SELECT COUNT(*) FROM events WHERE handled = 0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notify_targets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 3
        assert settings.get(conn, "ai.active_prompt") == pid
        assert conn.execute("SELECT name FROM prompts").fetchone()["name"] == "mine"
        assert conn.execute("SELECT COUNT(*) FROM ai_servers WHERE connected = 1").fetchone()[0] == 1

    # undo: restore the copy saved before the restore
    with db.session() as conn, db.transaction(conn):
        conn.execute("DELETE FROM notify_targets")
    c.post(f"/api/backup/files/{r.json()['saved_first']}/restore", json={"groups": ["notifications"]})
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notify_targets").fetchone()[0] == 0  # as it was before the restore


def test_upload_download_and_bad_files(c):
    name = c.post("/api/backup/now").json()["name"]
    data = c.get(f"/api/backup/files/{name}").content
    assert data[:2] == b"PK"
    up = c.post("/api/backup/upload", content=data, headers={"Content-Type": "application/zip"}).json()
    assert up["name"].startswith("dockllama-uploaded-")
    assert c.get(f"/api/backup/files/{up['name']}/contents").json()["counts"]["ai"] == 1
    assert c.post("/api/backup/upload", content=b"not a zip").status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "dockllama", "schema": db.LATEST + 1}))
        z.writestr("dockllama.db", b"")
    r = c.post("/api/backup/upload", content=buf.getvalue())
    assert r.status_code == 400 and "newer DockLlama" in r.json()["detail"]
    assert c.get("/api/backup/files/..%2Fdockllama.db").status_code in (400, 404)
    assert c.get("/api/backup/files/evil.zip").status_code == 400
    assert c.delete(f"/api/backup/files/{up['name']}").json() == {"ok": True}


def test_older_backup_is_upgraded_before_restoring(c, data_dir):
    """A backup made by an older DockLlama (older schema) still restores."""
    old_db = data_dir / "old" / "dockllama.db"
    old_db.parent.mkdir()
    with db.session(old_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for i, m in enumerate(db.MIGRATIONS[:8], 1):  # schema v8: before Qwen labels and prompts
            with db.transaction(conn):
                m(conn)
                conn.execute("UPDATE schema_version SET version = ?", (i,))
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('checks.interval_minutes', '45', 0)")
        conn.execute("PRAGMA journal_mode=DELETE")
    target = paths.USER_BACKUP_DIR / "dockllama-uploaded-20250101-000000.zip"
    paths.USER_BACKUP_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "dockllama", "schema": 8, "created": 0}))
        z.write(old_db, "dockllama.db")
    r = c.post(f"/api/backup/files/{target.name}/restore", json={"groups": ["checking"]})
    assert r.status_code == 200, r.text
    with db.session() as conn:
        assert settings.get(conn, "checks.interval_minutes") == 45


def test_automatic_backups_follow_the_schedule_and_keep_count(c, monkeypatch):
    c.put("/api/backup", json={"keep": 3})
    with db.session() as conn, db.transaction(conn):
        settings.set(conn, "setup.complete", True)
    assert backup.AutoBackup.due()
    for i in range(5):
        with monkeypatch.context() as m:
            m.setattr(backup, "_new_name", lambda kind, i=i: paths.USER_BACKUP_DIR / f"dockllama-{kind}-2026010{i}-000000.zip")
            backup.make("auto")
    autos = [f for f in backup.list_files() if f["kind"] == "auto"]
    assert len(autos) == 3
    assert not backup.AutoBackup.due()  # one was just made
    c.put("/api/backup", json={"auto": False})
    assert not backup.AutoBackup.due()
    assert c.put("/api/backup", json={"every_hours": 5}).status_code == 400


def test_restore_refused_while_a_model_test_runs(c, fake):
    import asyncio
    gate = {"open": False}

    async def hold(payload, request):
        while not gate["open"]:
            await asyncio.sleep(0.01)
        from .fake_ollama import perfect_answer
        return perfect_answer(payload)
    name = c.post("/api/backup/now").json()["name"]
    fake.servers["thor"].generate_handler = hold
    c.post("/api/ai/tests", json={"names": ["gemma4:latest"]})
    assert c.post(f"/api/backup/files/{name}/restore", json={"groups": ["ai"]}).status_code == 409
    assert c.post(f"/api/backup/files/{name}/restore", json={"groups": ["checking"]}).status_code == 200
    gate["open"] = True
    wait_idle(c)
