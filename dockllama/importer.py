"""Import a DockLlama v1 setup (phase 11): its config.yaml and, optionally, its old database.

What comes across:
- containers: watched or not, notes for the AI (v1 "context prompt"; the old database's version wins,
  as it did in v1), ignore patterns, known patterns, model override;
- restart groups: v1 dependency_groups (in their order), otherwise compose_group;
- alert URLs (config and old database) as notification targets (Apprise URLs work unchanged);
- check interval, restarts per hour, wait between restarts, daily summary time.
What doesn't: v1's Ollama address and model (connect it in Settings → AI, which tests it first),
and history. DockLlama stays in Watch only; containers v1 was allowed to restart get "allow
automatic restart" so turning on Auto-restart restores v1's behaviour.
Running it twice is safe: it updates rather than duplicates.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

import yaml

from . import settings


class ImportError_(ValueError):
    """The file couldn't be read as a v1 config, worded for the user."""


@dataclass
class Plan:
    containers: list[dict] = field(default_factory=list)
    alert_urls: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # things the user should know or do


def _cron_to_time(cron: str) -> str | None:
    """'0 7 * * *' → '07:00' (only plain daily schedules)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*", cron or "")
    if not m:
        return None
    minute, hour = int(m.group(1)), int(m.group(2))
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def read_old_db(path: str) -> tuple[dict, list[str]]:
    """Per-container prompts and alert URLs from a v1 database (read-only)."""
    prompts, urls = {}, []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "container_prompts" in tables:
            for r in con.execute("SELECT * FROM container_prompts"):
                prompts[r["container"]] = {
                    "context_prompt": r["context_prompt"],
                    "known_patterns": json.loads(r["known_patterns"]) if r["known_patterns"] else None,
                }
        if "alert_urls" in tables:
            cols = [c["name"] for c in con.execute("PRAGMA table_info(alert_urls)")]
            col = "url" if "url" in cols else cols[1]
            urls = [r[col] for r in con.execute(f"SELECT {col} FROM alert_urls") if r[col]]
    finally:
        con.close()
    return prompts, urls


def plan(config_text: str, old_db: dict | None = None, old_urls: list[str] | None = None) -> Plan:
    try:
        cfg = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as exc:
        raise ImportError_(f"That file isn't valid YAML: {exc}") from exc
    if not isinstance(cfg, dict) or not isinstance(cfg.get("containers"), list):
        raise ImportError_("That doesn't look like a DockLlama v1 config.yaml (it has no containers list).")
    p = Plan()
    old_db = old_db or {}
    dry_run = bool((cfg.get("monitoring") or {}).get("dry_run", True))
    groups: dict[str, tuple[str, int]] = {}
    for gname, members in (cfg.get("dependency_groups") or {}).items():
        for order, name in enumerate(members or []):
            groups[str(name)] = (str(gname).lower()[:40], order)
    other_nodes = set()
    for c in cfg["containers"]:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        name = str(c["name"])
        if c.get("node", "local") != "local":
            other_nodes.add(c["node"])
            continue
        db_entry = old_db.get(name) or {}
        notes = db_entry.get("context_prompt") or c.get("context_prompt") or ""
        known = db_entry.get("known_patterns") if db_entry.get("known_patterns") is not None else (c.get("known_patterns") or [])
        group, order = groups.get(name, ((c.get("compose_group") or "").lower()[:40], 0))
        p.containers.append({
            "name": name, "monitored": bool(c.get("enabled", True)), "notes_for_ai": str(notes)[:4000],
            "ignore_patterns": [str(x) for x in (c.get("ignore_patterns") or [])],
            "known_patterns": [k for k in known if isinstance(k, dict) and k.get("pattern")],
            "model_override": c.get("model_override") or None, "restart_group": group, "restart_order": order,
            "allow_auto_restart": not dry_run and bool(c.get("enabled", True)),
        })
    urls = [str(u) for u in ((cfg.get("alerts") or {}).get("urls") or [])] + list(old_urls or [])
    p.alert_urls = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    mon, cool = cfg.get("monitoring") or {}, cfg.get("cooldowns") or {}
    if mon.get("poll_interval_seconds"):
        p.settings["checks.interval_minutes"] = max(1, min(1440, round(int(mon["poll_interval_seconds"]) / 60)))
    if mon.get("log_lines_per_check"):
        p.settings["checks.log_lines"] = max(20, min(2000, int(mon["log_lines_per_check"])))
    if cool.get("max_restarts_per_hour"):
        p.settings["fixing.max_per_hour"] = max(1, min(20, int(cool["max_restarts_per_hour"])))
    if cool.get("initial_minutes"):
        p.settings["fixing.cooldown_minutes"] = max(1, min(1440, int(cool["initial_minutes"])))
    digest = cfg.get("digest") or {}
    if "enabled" in digest:
        p.settings["summary.enabled"] = bool(digest["enabled"])
    t = _cron_to_time(digest.get("schedule_cron", ""))
    if t:
        p.settings["summary.time"] = t
    ollama = cfg.get("ollama") or {}
    if ollama.get("base_url"):
        p.notes.append(f"v1 used Ollama at {ollama['base_url']} with {ollama.get('default_model', 'its default model')}. "
                       "Connect it in Settings → AI; DockLlama checks it and suggests a model.")
    if other_nodes:
        p.notes.append("Containers on other Docker hosts (" + ", ".join(sorted(other_nodes)) + ") weren't imported. "
                       "Add those machines in Settings → Machines, then pick their containers.")
    if not dry_run:
        p.notes.append("v1 restarted containers by itself. DockLlama starts in Watch only; the containers v1 could "
                       "restart are allowed to, so switching to Auto-restart in Settings → Checking and fixing restores that.")
    return p


def apply(conn, p: Plan, machine_id: int = 1) -> dict:
    """Write the plan. Caller holds a transaction."""
    now = time.time()
    added = updated = 0
    for c in p.containers:
        row = conn.execute("SELECT id FROM containers WHERE machine_id = ? AND name = ?", (machine_id, c["name"])).fetchone()
        values = (int(c["monitored"]), c["notes_for_ai"], json.dumps(c["ignore_patterns"]), json.dumps(c["known_patterns"]),
                  c["model_override"], c["restart_group"], c["restart_order"], int(c["allow_auto_restart"]))
        if row is None:
            conn.execute("""INSERT INTO containers (machine_id, name, monitored, notes_for_ai, ignore_patterns, known_patterns,
                                model_override, restart_group, restart_order, allow_auto_restart, first_seen, last_seen)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (machine_id, c["name"], *values, now, now))
            added += 1
        else:
            conn.execute("""UPDATE containers SET monitored = ?, notes_for_ai = ?, ignore_patterns = ?, known_patterns = ?,
                                model_override = ?, restart_group = ?, restart_order = ?, allow_auto_restart = ?
                            WHERE id = ?""", (*values, row["id"]))
            updated += 1
    existing = {r["url"] for r in conn.execute("SELECT url FROM notify_targets")}
    targets = 0
    for url in p.alert_urls:
        if url in existing:
            continue
        conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) VALUES ('apprise', ?, ?, '{}', ?)",
                     (json.dumps({"url": url}), url, now))
        targets += 1
    for key, value in p.settings.items():
        settings.set(conn, key, value)
    return {"containers_added": added, "containers_updated": updated, "notify_targets_added": targets,
            "settings": p.settings, "notes": p.notes}
