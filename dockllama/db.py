"""SQLite database layer for DockLlama."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = "/app/data/dockllama.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL,
    ai_status TEXT,
    confidence INTEGER,
    root_cause_category TEXT,
    summary TEXT,
    action_taken TEXT,
    log_snapshot TEXT,
    prompt_version TEXT,
    model_used TEXT,
    health_score INTEGER
);

CREATE TABLE IF NOT EXISTS cooldowns (
    container TEXT PRIMARY KEY,
    last_restart TEXT NOT NULL,
    consecutive_restarts INTEGER DEFAULT 0,
    current_cooldown_minutes INTEGER DEFAULT 5,
    alert_only_mode INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS baselines (
    container TEXT PRIMARY KEY,
    healthy_log_sample TEXT,
    captured_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_container ON events(container);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    overall_health TEXT,
    headline TEXT,
    digest_json TEXT NOT NULL,
    formatted_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_digests_date ON digests(date);

CREATE TABLE IF NOT EXISTS alert_urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tested_models (
    model TEXT PRIMARY KEY,
    tested_at TEXT NOT NULL DEFAULT (datetime('now')),
    healthy_pass INTEGER DEFAULT 0,
    failing_pass INTEGER DEFAULT 0,
    avg_response_ms INTEGER DEFAULT 0,
    status TEXT DEFAULT 'untested',
    results_json TEXT
);


CREATE TABLE IF NOT EXISTS container_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_percent REAL,
    mem_percent REAL,
    mem_usage_mb REAL,
    net_rx_bytes INTEGER,
    net_tx_bytes INTEGER
);

CREATE INDEX IF NOT EXISTS idx_stats_container_time ON container_stats(container, timestamp);
CREATE TABLE IF NOT EXISTS container_prompts (
    container TEXT PRIMARY KEY,
    context_prompt TEXT,
    examples TEXT,
    known_patterns TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS health_checks (
    container TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'GET',
    expected_status INTEGER NOT NULL DEFAULT 200,
    interval_seconds INTEGER NOT NULL DEFAULT 60,
    timeout_seconds INTEGER NOT NULL DEFAULT 10,
    failure_threshold INTEGER NOT NULL DEFAULT 3,
    enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health_check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    status_code INTEGER,
    response_ms INTEGER,
    success INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_hc_results_container_time ON health_check_results(container, timestamp);

CREATE TABLE IF NOT EXISTS blackout_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    days TEXT NOT NULL DEFAULT '[]',
    start_time TEXT NOT NULL DEFAULT '00:00',
    end_time TEXT NOT NULL DEFAULT '23:59',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS container_config_archive (
    container TEXT PRIMARY KEY,
    ignore_patterns TEXT,
    compose_group TEXT,
    model_override TEXT,
    enabled INTEGER DEFAULT 1,
    archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode enabled."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Initialize the database with the schema."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    # Migrations for existing installs
    for sql in [
        "ALTER TABLE tested_models ADD COLUMN results_json TEXT",
    ]:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass  # column already exists
    return conn


def verify_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """Return row counts for all tables."""
    tables = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        tables[name] = count
    return tables


def archive_container_config(conn, container: str, ignore_patterns: list = None,
                              compose_group: str = None, model_override: str = None,
                              enabled: bool = True) -> None:
    """Archive a container's config.yaml settings to DB before removal."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    patterns_json = json.dumps(ignore_patterns) if ignore_patterns else None
    conn.execute(
        """INSERT INTO container_config_archive
           (container, ignore_patterns, compose_group, model_override, enabled, archived_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(container) DO UPDATE SET
             ignore_patterns = excluded.ignore_patterns,
             compose_group = excluded.compose_group,
             model_override = excluded.model_override,
             enabled = excluded.enabled,
             archived_at = excluded.archived_at""",
        (container, patterns_json, compose_group, model_override, int(enabled), now),
    )
    conn.commit()


def restore_container_config(conn, container: str) -> dict | None:
    """Restore archived config for a container. Returns None if no archive exists."""
    row = conn.execute(
        "SELECT ignore_patterns, compose_group, model_override, enabled, archived_at "
        "FROM container_config_archive WHERE container = ?",
        (container,),
    ).fetchone()
    if not row:
        return None
    return {
        "container": container,
        "ignore_patterns": json.loads(row[0]) if row[0] else [],
        "compose_group": row[1],
        "model_override": row[2],
        "enabled": bool(row[3]),
        "archived_at": row[4],
    }


def purge_container_data(conn, container: str) -> dict:
    """Delete ALL data for a container from every table. Returns counts of deleted rows."""
    deleted = {}
    for table, col in [
        ("events", "container"),
        ("container_stats", "container"),
        ("baselines", "container"),
        ("cooldowns", "container"),
        ("container_prompts", "container"),
        ("container_config_archive", "container"),
    ]:
        cursor = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (container,))
        if cursor.rowcount:
            deleted[table] = cursor.rowcount
    conn.commit()
    return deleted


def get_container_prompt(conn, container: str) -> dict | None:
    """Fetch prompt overrides for a container from DB. Returns None if not set."""
    row = conn.execute(
        "SELECT context_prompt, examples, known_patterns, updated_at "
        "FROM container_prompts WHERE container = ?",
        (container,),
    ).fetchone()
    if not row:
        return None
    return {
        "container": container,
        "context_prompt": row[0],
        "examples": json.loads(row[1]) if row[1] else [],
        "known_patterns": json.loads(row[2]) if row[2] else [],
        "updated_at": row[3],
    }


def save_container_prompt(conn, container: str, context_prompt: str | None,
                          examples: list | None, known_patterns: list | None) -> None:
    """Save or update prompt overrides for a container."""
    now = datetime.now(timezone.utc).isoformat()
    examples_json = json.dumps(examples) if examples else None
    patterns_json = json.dumps(known_patterns) if known_patterns else None
    conn.execute(
        """INSERT INTO container_prompts (container, context_prompt, examples, known_patterns, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(container) DO UPDATE SET
             context_prompt = excluded.context_prompt,
             examples = excluded.examples,
             known_patterns = excluded.known_patterns,
             updated_at = excluded.updated_at""",
        (container, context_prompt, examples_json, patterns_json, now),
    )
    conn.commit()


def delete_container_prompt(conn, container: str) -> bool:
    """Delete prompt overrides for a container. Returns True if a row was deleted."""
    cursor = conn.execute("DELETE FROM container_prompts WHERE container = ?", (container,))
    conn.commit()
    return cursor.rowcount > 0



def get_tested_models(conn) -> list[dict]:
    """Get all tested model records."""
    rows = conn.execute(
        "SELECT model, tested_at, healthy_pass, failing_pass, avg_response_ms, status "
        "FROM tested_models ORDER BY tested_at DESC"
    ).fetchall()
    return [
        {"model": r[0], "tested_at": r[1], "healthy_pass": bool(r[2]),
         "failing_pass": bool(r[3]), "avg_response_ms": r[4], "status": r[5]}
        for r in rows
    ]


def save_tested_model(conn, model: str, healthy_pass: bool, failing_pass: bool,
                       avg_response_ms: int, results_json: str = None) -> None:
    """Save or update a model test result."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status = "supported" if (healthy_pass and failing_pass) else "failed"
    conn.execute(
        """INSERT INTO tested_models (model, tested_at, healthy_pass, failing_pass, avg_response_ms, status, results_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(model) DO UPDATE SET
             tested_at = excluded.tested_at,
             healthy_pass = excluded.healthy_pass,
             failing_pass = excluded.failing_pass,
             avg_response_ms = excluded.avg_response_ms,
             status = excluded.status,
             results_json = excluded.results_json""",
        (model, now, int(healthy_pass), int(failing_pass), avg_response_ms, status, results_json),
    )
    conn.commit()


def save_container_stats(conn, container: str, cpu_percent, mem_percent,
                         mem_usage_mb, net_rx_bytes, net_tx_bytes) -> None:
    """Record one stats snapshot for a container."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO container_stats (container, timestamp, cpu_percent, mem_percent, mem_usage_mb, net_rx_bytes, net_tx_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (container, now, cpu_percent, mem_percent, mem_usage_mb, net_rx_bytes, net_tx_bytes),
    )
    conn.commit()


def prune_container_stats(conn, retention_days: int = 7) -> int:
    """Delete stats older than retention_days. Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("DELETE FROM container_stats WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def prune_old_events(conn, retention_days=90):
    """Delete events older than retention_days. Returns rows deleted."""
    import logging
    logger = logging.getLogger(__name__)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    if deleted:
        logger.info("Pruned %d events older than %d days", deleted, retention_days)
    return deleted


def vacuum_db(conn):
    """Reclaim space after pruning."""
    conn.execute("VACUUM")


# --- Health Check CRUD ---

def get_health_check(conn, container: str) -> dict | None:
    """Get health check config for a container."""
    row = conn.execute(
        "SELECT container, url, method, expected_status, interval_seconds, "
        "timeout_seconds, failure_threshold, enabled FROM health_checks WHERE container = ?",
        (container,),
    ).fetchone()
    if not row:
        return None
    return {
        "container": row[0], "url": row[1], "method": row[2],
        "expected_status": row[3], "interval_seconds": row[4],
        "timeout_seconds": row[5], "failure_threshold": row[6],
        "enabled": bool(row[7]),
    }


def get_enabled_health_checks(conn) -> list[dict]:
    """Get enabled health check configs (for the poller)."""
    rows = conn.execute(
        "SELECT container, url, method, expected_status, interval_seconds, "
        "timeout_seconds, failure_threshold, enabled FROM health_checks WHERE enabled = 1"
    ).fetchall()
    return [
        {"container": r[0], "url": r[1], "method": r[2],
         "expected_status": r[3], "interval_seconds": r[4],
         "timeout_seconds": r[5], "failure_threshold": r[6],
         "enabled": bool(r[7])}
        for r in rows
    ]


def get_all_health_checks(conn) -> list[dict]:
    """Get ALL health check configs (enabled and disabled) for API listing."""
    rows = conn.execute(
        "SELECT container, url, method, expected_status, interval_seconds, "
        "timeout_seconds, failure_threshold, enabled FROM health_checks"
    ).fetchall()
    return [
        {"container": r[0], "url": r[1], "method": r[2],
         "expected_status": r[3], "interval_seconds": r[4],
         "timeout_seconds": r[5], "failure_threshold": r[6],
         "enabled": bool(r[7])}
        for r in rows
    ]


def save_health_check(conn, container: str, url: str, method: str = "GET",
                       expected_status: int = 200, interval_seconds: int = 60,
                       timeout_seconds: int = 10, failure_threshold: int = 3,
                       enabled: bool = False) -> None:
    """Save or update a health check config."""
    conn.execute(
        """INSERT INTO health_checks
           (container, url, method, expected_status, interval_seconds, timeout_seconds, failure_threshold, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(container) DO UPDATE SET
             url = excluded.url, method = excluded.method,
             expected_status = excluded.expected_status,
             interval_seconds = excluded.interval_seconds,
             timeout_seconds = excluded.timeout_seconds,
             failure_threshold = excluded.failure_threshold,
             enabled = excluded.enabled""",
        (container, url, method, expected_status, interval_seconds, timeout_seconds,
         failure_threshold, int(enabled)),
    )
    conn.commit()


def delete_health_check(conn, container: str) -> bool:
    """Delete health check config. Returns True if deleted."""
    cursor = conn.execute("DELETE FROM health_checks WHERE container = ?", (container,))
    conn.commit()
    return cursor.rowcount > 0


def save_health_check_result(conn, container: str, status_code: int | None,
                              response_ms: int, success: bool, error: str | None = None) -> None:
    """Record a single health check result."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO health_check_results (container, timestamp, status_code, response_ms, success, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (container, now, status_code, response_ms, int(success), error),
    )
    conn.commit()


def get_health_check_history(conn, container: str, limit: int = 20) -> list[dict]:
    """Get recent health check results for a container."""
    rows = conn.execute(
        "SELECT timestamp, status_code, response_ms, success, error "
        "FROM health_check_results WHERE container = ? ORDER BY id DESC LIMIT ?",
        (container, limit),
    ).fetchall()
    return [
        {"timestamp": r[0], "status_code": r[1], "response_ms": r[2],
         "success": bool(r[3]), "error": r[4]}
        for r in rows
    ]


# ─── Blackout Windows ─────────────────────────────────────────────

def get_all_blackout_windows(conn) -> list[dict]:
    """Get all blackout windows."""
    rows = conn.execute("SELECT id, name, days, start_time, end_time, enabled FROM blackout_windows ORDER BY name").fetchall()
    return [
        {"id": r[0], "name": r[1], "days": json.loads(r[2]), "start_time": r[3], "end_time": r[4], "enabled": bool(r[5])}
        for r in rows
    ]


def get_blackout_window(conn, window_id: int) -> dict | None:
    """Get a single blackout window by ID."""
    r = conn.execute("SELECT id, name, days, start_time, end_time, enabled FROM blackout_windows WHERE id = ?", (window_id,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "days": json.loads(r[2]), "start_time": r[3], "end_time": r[4], "enabled": bool(r[5])}


def save_blackout_window(conn, name: str, days: list[int], start_time: str, end_time: str,
                         enabled: bool = True, window_id: int | None = None) -> int:
    """Create or update a blackout window. Returns the window ID."""
    days_json = json.dumps(days)
    if window_id:
        conn.execute(
            "UPDATE blackout_windows SET name=?, days=?, start_time=?, end_time=?, enabled=? WHERE id=?",
            (name, days_json, start_time, end_time, int(enabled), window_id),
        )
        conn.commit()
        return window_id
    else:
        cursor = conn.execute(
            "INSERT INTO blackout_windows (name, days, start_time, end_time, enabled) VALUES (?, ?, ?, ?, ?)",
            (name, days_json, start_time, end_time, int(enabled)),
        )
        conn.commit()
        return cursor.lastrowid


def delete_blackout_window(conn, window_id: int) -> bool:
    """Delete a blackout window. Returns True if deleted."""
    cursor = conn.execute("DELETE FROM blackout_windows WHERE id = ?", (window_id,))
    conn.commit()
    return cursor.rowcount > 0


def is_blackout_active(conn, now: datetime | None = None) -> dict | None:
    """Check if any blackout window is currently active.
    Returns the active window dict, or None if no blackout is active.
    Supports overnight spans (start_time > end_time, e.g. 22:00-06:00).
    Days are weekday ints: 0=Monday, 6=Sunday.
    """
    if now is None:
        now = datetime.now()

    current_day = now.weekday()  # 0=Monday
    current_time = now.time()

    windows = conn.execute(
        "SELECT id, name, days, start_time, end_time FROM blackout_windows WHERE enabled = 1"
    ).fetchall()

    for row in windows:
        wid, name, days_json, start_str, end_str = row
        days = json.loads(days_json)
        start = dtime.fromisoformat(start_str)
        end = dtime.fromisoformat(end_str)

        if start <= end:
            # Same-day window: e.g. 02:00-06:00
            if current_day in days and start <= current_time <= end:
                return {"id": wid, "name": name, "days": days, "start_time": start_str, "end_time": end_str}
        else:
            # Overnight window: e.g. 22:00-06:00
            # Active if: (today in days AND time >= start) OR (yesterday in days AND time <= end)
            if current_day in days and current_time >= start:
                return {"id": wid, "name": name, "days": days, "start_time": start_str, "end_time": end_str}
            yesterday = (current_day - 1) % 7
            if yesterday in days and current_time <= end:
                return {"id": wid, "name": name, "days": days, "start_time": start_str, "end_time": end_str}

    return None


if __name__ == "__main__":
    import tempfile
    import os

    test_path = os.path.join(tempfile.gettempdir(), "dockllama_test.db")
    print(f"Testing DB at {test_path}")

    conn = init_db(test_path)
    tables = verify_tables(conn)
    print(f"Tables created: {tables}")

    # Verify WAL mode
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"Journal mode: {mode}")

    conn.close()
    os.unlink(test_path)
    print("Test passed, cleaned up.")
