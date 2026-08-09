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

CREATE TABLE IF NOT EXISTS benchmark_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    tested_at TEXT NOT NULL DEFAULT (datetime('now')),
    benchmark_version TEXT DEFAULT '2.0',
    total_score INTEGER DEFAULT 0,
    max_score INTEGER DEFAULT 0,
    percentage REAL DEFAULT 0,
    grade TEXT DEFAULT '',
    avg_response_ms INTEGER DEFAULT 0,
    results_json TEXT,
    tier TEXT DEFAULT ''
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

CREATE TABLE IF NOT EXISTS base_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prompt_type TEXT NOT NULL,
    content TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    is_system_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_base_prompts_type ON base_prompts(prompt_type);

CREATE TABLE IF NOT EXISTS log_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    hours_evaluated INTEGER NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0,
    strategy_used TEXT,
    output_format TEXT NOT NULL DEFAULT 'report',
    model TEXT,
    result_json TEXT,
    eval_time_seconds REAL,
    prompt_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_evals_container ON log_evaluations(container);
CREATE INDEX IF NOT EXISTS idx_log_evals_timestamp ON log_evaluations(timestamp);

CREATE TABLE IF NOT EXISTS eval_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    eval_type TEXT NOT NULL,
    status TEXT,
    health_score INTEGER,
    confidence INTEGER,
    summary TEXT,
    root_cause_category TEXT,
    error_origin TEXT,
    recommended_action TEXT,
    restart_would_help INTEGER DEFAULT 0,
    restart_reasoning TEXT,
    model TEXT,
    prompt_version TEXT,
    lines_evaluated INTEGER,
    eval_time_seconds REAL,
    full_result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_history_container_time ON eval_history(container, timestamp);
CREATE INDEX IF NOT EXISTS idx_eval_history_type ON eval_history(eval_type);

CREATE TABLE IF NOT EXISTS container_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    sample_log_line TEXT,
    classification TEXT NOT NULL DEFAULT 'expected',
    user_explanation TEXT,
    times_seen INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_container_hash ON container_knowledge(container, pattern_hash);

CREATE TABLE IF NOT EXISTS pending_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    sample_log_line TEXT,
    occurrence_count INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    ai_guess TEXT,
    surfaced INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_container_hash ON pending_patterns(container, pattern_hash);

CREATE TABLE IF NOT EXISTS container_trend_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    analysis_date TEXT NOT NULL DEFAULT (datetime('now')),
    period_days INTEGER NOT NULL DEFAULT 7,
    eval_count INTEGER DEFAULT 0,
    avg_health_score REAL,
    min_health_score INTEGER,
    max_health_score INTEGER,
    healthy_pct REAL DEFAULT 0,
    degraded_pct REAL DEFAULT 0,
    unhealthy_pct REAL DEFAULT 0,
    recurring_root_causes TEXT,
    score_trend TEXT DEFAULT 'stable',
    analysis_summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trend_analysis_container ON container_trend_analysis(container, analysis_date);
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
        """CREATE TABLE IF NOT EXISTS eval_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            eval_type TEXT NOT NULL,
            status TEXT,
            health_score INTEGER,
            confidence INTEGER,
            summary TEXT,
            root_cause_category TEXT,
            error_origin TEXT,
            recommended_action TEXT,
            restart_would_help INTEGER DEFAULT 0,
            restart_reasoning TEXT,
            model TEXT,
            prompt_version TEXT,
            lines_evaluated INTEGER,
            eval_time_seconds REAL,
            full_result_json TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_eval_history_container_time ON eval_history(container, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_eval_history_type ON eval_history(eval_type)",
    ]:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass  # column already exists
    # Migration: node_name columns
    for tbl in ["events", "eval_history"]:
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN node_name TEXT DEFAULT 'local'")
            conn.commit()
        except Exception:
            pass
    # Migration: baselines.image_id column
    try:
        conn.execute("ALTER TABLE baselines ADD COLUMN image_id TEXT")
        conn.commit()
    except Exception:
        pass
    # Migration: container_trend_analysis table
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS container_trend_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container TEXT NOT NULL,
                analysis_date TEXT NOT NULL DEFAULT (datetime('now')),
                period_days INTEGER NOT NULL DEFAULT 7,
                eval_count INTEGER DEFAULT 0,
                avg_health_score REAL,
                min_health_score INTEGER,
                max_health_score INTEGER,
                healthy_pct REAL DEFAULT 0,
                degraded_pct REAL DEFAULT 0,
                unhealthy_pct REAL DEFAULT 0,
                recurring_root_causes TEXT,
                score_trend TEXT DEFAULT 'stable',
                analysis_summary TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_trend_analysis_container ON container_trend_analysis(container, analysis_date);
        """)
        conn.commit()
    except Exception:
        pass
    # Migration: pending_patterns table
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                sample_log_line TEXT,
                occurrence_count INTEGER DEFAULT 1,
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                ai_guess TEXT,
                surfaced INTEGER DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_container_hash ON pending_patterns(container, pattern_hash);
        """)
        conn.commit()
    except Exception:
        pass
    # Migration: container_knowledge table
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS container_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                sample_log_line TEXT,
                classification TEXT NOT NULL DEFAULT 'expected',
                user_explanation TEXT,
                times_seen INTEGER DEFAULT 1,
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_container_hash ON container_knowledge(container, pattern_hash);
        """)
        conn.commit()
    except Exception:
        pass
    seed_base_prompts(conn)
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
        ("eval_history", "container"),
        ("container_knowledge", "container"),
        ("pending_patterns", "container"),
        ("container_trend_analysis", "container"),
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




def save_benchmark_result(conn, model: str, tier: str, total_score: int, max_score: int,
                          percentage: float, grade: str, avg_ms: int, results_json: str):
    """Save or update benchmark result for a model."""
    conn.execute(
        """INSERT OR REPLACE INTO benchmark_results
        (model, tested_at, benchmark_version, total_score, max_score, percentage, grade,
         avg_response_ms, results_json, tier)
        VALUES (?, datetime('now'), '2.0', ?, ?, ?, ?, ?, ?, ?)""",
        (model, total_score, max_score, percentage, grade, avg_ms, results_json, tier),
    )
    conn.commit()


def get_benchmark_results(conn) -> list:
    """Get all benchmark results."""
    rows = conn.execute(
        "SELECT model, tested_at, total_score, max_score, percentage, grade, "
        "avg_response_ms, results_json, tier FROM benchmark_results ORDER BY percentage DESC"
    ).fetchall()
    return [
        {"model": r[0], "tested_at": r[1], "total_score": r[2], "max_score": r[3],
         "percentage": r[4], "grade": r[5], "avg_response_ms": r[6],
         "results_json": r[7], "tier": r[8]}
        for r in rows
    ]



# ─── Base Prompts ──────────────────────────────────────────────

DEFAULT_PROMPTS = {}  # populated by seed_base_prompts on first call


def seed_base_prompts(conn) -> None:
    """Seed the base_prompts table with hardcoded defaults if empty."""
    from pathlib import Path
    prompts_dir = Path(__file__).parent / "prompts"

    defaults = [
        ("Default Health Eval", "eval", "v5_evaluate.txt"),
        ("Default Log Evaluation", "log_eval", "v1_log_evaluate.txt"),
        ("Default Digest", "digest", "v1_digest.txt"),
    ]

    for name, ptype, filename in defaults:
        existing = conn.execute(
            "SELECT id FROM base_prompts WHERE prompt_type = ? AND is_system_default = 1",
            (ptype,),
        ).fetchone()
        if existing:
            continue

        filepath = prompts_dir / filename
        if not filepath.exists():
            continue

        file_content = filepath.read_text().strip()
        conn.execute(
            """INSERT INTO base_prompts (name, prompt_type, content, is_active, is_system_default)
               VALUES (?, ?, ?, 1, 1)""",
            (name, ptype, file_content),
        )
    conn.commit()


def get_active_base_prompt(conn, prompt_type: str) -> dict | None:
    """Get the active base prompt for a given type (eval, log_eval, digest)."""
    row = conn.execute(
        "SELECT id, name, prompt_type, content, is_active, is_system_default, created_at, updated_at "
        "FROM base_prompts WHERE prompt_type = ? AND is_active = 1 LIMIT 1",
        (prompt_type,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "prompt_type": row[2], "content": row[3],
        "is_active": bool(row[4]), "is_system_default": bool(row[5]),
        "created_at": row[6], "updated_at": row[7],
    }


def get_all_base_prompts(conn, prompt_type: str = None) -> list[dict]:
    """Get all base prompts, optionally filtered by type."""
    if prompt_type:
        rows = conn.execute(
            "SELECT id, name, prompt_type, content, is_active, is_system_default, created_at, updated_at "
            "FROM base_prompts WHERE prompt_type = ? ORDER BY is_active DESC, name",
            (prompt_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, prompt_type, content, is_active, is_system_default, created_at, updated_at "
            "FROM base_prompts ORDER BY prompt_type, is_active DESC, name"
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "prompt_type": r[2], "content": r[3],
         "is_active": bool(r[4]), "is_system_default": bool(r[5]),
         "created_at": r[6], "updated_at": r[7]}
        for r in rows
    ]


def get_base_prompt(conn, prompt_id: int) -> dict | None:
    """Get a single base prompt by ID."""
    row = conn.execute(
        "SELECT id, name, prompt_type, content, is_active, is_system_default, created_at, updated_at "
        "FROM base_prompts WHERE id = ?",
        (prompt_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "prompt_type": row[2], "content": row[3],
        "is_active": bool(row[4]), "is_system_default": bool(row[5]),
        "created_at": row[6], "updated_at": row[7],
    }


def save_base_prompt(conn, name: str, prompt_type: str, content: str,
                     prompt_id: int = None) -> int:
    """Create or update a base prompt. Returns the prompt ID."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if prompt_id:
        conn.execute(
            "UPDATE base_prompts SET name = ?, content = ?, updated_at = ? WHERE id = ?",
            (name, content, now, prompt_id),
        )
        conn.commit()
        return prompt_id
    else:
        cursor = conn.execute(
            "INSERT INTO base_prompts (name, prompt_type, content, is_active, is_system_default, updated_at) "
            "VALUES (?, ?, ?, 0, 0, ?)",
            (name, prompt_type, content, now),
        )
        conn.commit()
        return cursor.lastrowid


def activate_base_prompt(conn, prompt_id: int) -> None:
    """Set a prompt as active for its type, deactivating others of the same type."""
    row = conn.execute("SELECT prompt_type FROM base_prompts WHERE id = ?", (prompt_id,)).fetchone()
    if not row:
        raise ValueError(f"Prompt {prompt_id} not found")
    ptype = row[0]
    conn.execute("UPDATE base_prompts SET is_active = 0 WHERE prompt_type = ?", (ptype,))
    conn.execute("UPDATE base_prompts SET is_active = 1 WHERE id = ?", (prompt_id,))
    conn.commit()


def delete_base_prompt(conn, prompt_id: int) -> bool:
    """Delete a base prompt. Cannot delete system defaults."""
    row = conn.execute(
        "SELECT is_system_default, is_active, prompt_type FROM base_prompts WHERE id = ?",
        (prompt_id,),
    ).fetchone()
    if not row:
        return False
    if row[0]:
        raise ValueError("Cannot delete system default prompts. Use reset instead.")
    was_active = row[1]
    ptype = row[2]
    conn.execute("DELETE FROM base_prompts WHERE id = ?", (prompt_id,))
    if was_active:
        conn.execute(
            "UPDATE base_prompts SET is_active = 1 WHERE prompt_type = ? AND is_system_default = 1",
            (ptype,),
        )
    conn.commit()
    return True


def reset_base_prompt(conn, prompt_type: str) -> None:
    """Reset a prompt type to its hardcoded default content and activate it."""
    from pathlib import Path
    from datetime import datetime, timezone
    prompts_dir = Path(__file__).parent / "prompts"
    filemap = {"eval": "v5_evaluate.txt", "log_eval": "v1_log_evaluate.txt", "digest": "v1_digest.txt"}
    filename = filemap.get(prompt_type)
    if not filename:
        raise ValueError(f"Unknown prompt type: {prompt_type}")
    filepath = prompts_dir / filename
    if not filepath.exists():
        raise FileNotFoundError(f"Default prompt file not found: {filepath}")
    file_content = filepath.read_text().strip()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE base_prompts SET is_active = 0 WHERE prompt_type = ?", (prompt_type,))
    conn.execute(
        "UPDATE base_prompts SET content = ?, is_active = 1, updated_at = ? "
        "WHERE prompt_type = ? AND is_system_default = 1",
        (file_content, now, prompt_type),
    )
    conn.commit()


# ─── Log Evaluations ──────────────────────────────────────────

def save_log_evaluation(conn, container: str, hours: int, line_count: int,
                        strategy: str, output_format: str, model: str,
                        result_json: str, eval_time: float, prompt_name: str = None) -> int:
    """Save a log evaluation result. Returns the evaluation ID."""
    cursor = conn.execute(
        """INSERT INTO log_evaluations
           (container, hours_evaluated, line_count, strategy_used, output_format,
            model, result_json, eval_time_seconds, prompt_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (container, hours, line_count, strategy, output_format, model,
         result_json, eval_time, prompt_name),
    )
    conn.commit()
    return cursor.lastrowid


def get_log_evaluations(conn, container: str, limit: int = 20) -> list[dict]:
    """Get recent log evaluations for a container."""
    rows = conn.execute(
        "SELECT id, container, timestamp, hours_evaluated, line_count, strategy_used, "
        "output_format, model, result_json, eval_time_seconds, prompt_name "
        "FROM log_evaluations WHERE container = ? ORDER BY id DESC LIMIT ?",
        (container, limit),
    ).fetchall()
    return [
        {"id": r[0], "container": r[1], "timestamp": r[2], "hours_evaluated": r[3],
         "line_count": r[4], "strategy_used": r[5], "output_format": r[6],
         "model": r[7], "result_json": r[8], "eval_time_seconds": r[9],
         "prompt_name": r[10]}
        for r in rows
    ]


def get_log_evaluation(conn, eval_id: int) -> dict | None:
    """Get a single log evaluation by ID."""
    r = conn.execute(
        "SELECT id, container, timestamp, hours_evaluated, line_count, strategy_used, "
        "output_format, model, result_json, eval_time_seconds, prompt_name "
        "FROM log_evaluations WHERE id = ?",
        (eval_id,),
    ).fetchone()
    if not r:
        return None
    return {
        "id": r[0], "container": r[1], "timestamp": r[2], "hours_evaluated": r[3],
        "line_count": r[4], "strategy_used": r[5], "output_format": r[6],
        "model": r[7], "result_json": r[8], "eval_time_seconds": r[9],
        "prompt_name": r[10],
    }


# ─── Eval History ──────────────────────────────────────────────

def save_eval_history(conn, container: str, eval_type: str, status: str,
                      health_score: int, confidence: int, summary: str,
                      root_cause_category: str = "none", error_origin: str = "none",
                      recommended_action: str = "none", restart_would_help: bool = False,
                      restart_reasoning: str = "", model: str = None,
                      prompt_version: str = None, lines_evaluated: int = 0,
                      eval_time_seconds: float = 0.0, full_result_json: str = None) -> int:
    """Save an evaluation result to eval_history. Returns the row ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        """INSERT INTO eval_history
           (container, timestamp, eval_type, status, health_score, confidence, summary,
            root_cause_category, error_origin, recommended_action, restart_would_help,
            restart_reasoning, model, prompt_version, lines_evaluated,
            eval_time_seconds, full_result_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (container, now, eval_type, status, health_score, confidence, summary,
         root_cause_category, error_origin, recommended_action,
         int(restart_would_help), restart_reasoning, model, prompt_version,
         lines_evaluated, eval_time_seconds, full_result_json),
    )
    conn.commit()
    return cursor.lastrowid


def get_eval_history(conn, container: str, eval_type: str = None,
                     limit: int = 50, since: str = None) -> list[dict]:
    """Get evaluation history for a container with optional filters."""
    query = ("SELECT id, container, timestamp, eval_type, status, health_score, "
             "confidence, summary, root_cause_category, error_origin, "
             "recommended_action, restart_would_help, restart_reasoning, "
             "model, prompt_version, lines_evaluated, eval_time_seconds, "
             "full_result_json "
             "FROM eval_history WHERE container = ?")
    params: list = [container]
    if eval_type:
        query += " AND eval_type = ?"
        params.append(eval_type)
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [
        {"id": r[0], "container": r[1], "timestamp": r[2], "eval_type": r[3],
         "status": r[4], "health_score": r[5], "confidence": r[6],
         "summary": r[7], "root_cause_category": r[8], "error_origin": r[9],
         "recommended_action": r[10], "restart_would_help": bool(r[11]),
         "restart_reasoning": r[12], "model": r[13], "prompt_version": r[14],
         "lines_evaluated": r[15], "eval_time_seconds": r[16],
         "full_result_json": r[17]}
        for r in rows
    ]


def get_eval_history_entry(conn, eval_id: int) -> dict | None:
    """Get a single eval history entry by ID."""
    r = conn.execute(
        "SELECT id, container, timestamp, eval_type, status, health_score, "
        "confidence, summary, root_cause_category, error_origin, "
        "recommended_action, restart_would_help, restart_reasoning, "
        "model, prompt_version, lines_evaluated, eval_time_seconds, "
        "full_result_json "
        "FROM eval_history WHERE id = ?",
        (eval_id,),
    ).fetchone()
    if not r:
        return None
    return {
        "id": r[0], "container": r[1], "timestamp": r[2], "eval_type": r[3],
        "status": r[4], "health_score": r[5], "confidence": r[6],
        "summary": r[7], "root_cause_category": r[8], "error_origin": r[9],
        "recommended_action": r[10], "restart_would_help": bool(r[11]),
        "restart_reasoning": r[12], "model": r[13], "prompt_version": r[14],
        "lines_evaluated": r[15], "eval_time_seconds": r[16],
        "full_result_json": r[17],
    }


def prune_eval_history(conn, retention_days: int = 10) -> int:
    """Delete eval_history rows older than retention_days. Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("DELETE FROM eval_history WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount



# ─── Container Knowledge Base ─────────────────────────────────

def get_container_knowledge(conn, container: str) -> list[dict]:
    """Get all knowledge entries for a container."""
    rows = conn.execute(
        "SELECT id, container, pattern_hash, sample_log_line, classification, "
        "user_explanation, times_seen, first_seen, last_seen, created_at, updated_at "
        "FROM container_knowledge WHERE container = ? ORDER BY last_seen DESC",
        (container,),
    ).fetchall()
    return [
        {"id": r[0], "container": r[1], "pattern_hash": r[2], "sample_log_line": r[3],
         "classification": r[4], "user_explanation": r[5], "times_seen": r[6],
         "first_seen": r[7], "last_seen": r[8], "created_at": r[9], "updated_at": r[10]}
        for r in rows
    ]


def get_knowledge_entry(conn, entry_id: int) -> dict | None:
    """Get a single knowledge entry by ID."""
    r = conn.execute(
        "SELECT id, container, pattern_hash, sample_log_line, classification, "
        "user_explanation, times_seen, first_seen, last_seen, created_at, updated_at "
        "FROM container_knowledge WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if not r:
        return None
    return {
        "id": r[0], "container": r[1], "pattern_hash": r[2], "sample_log_line": r[3],
        "classification": r[4], "user_explanation": r[5], "times_seen": r[6],
        "first_seen": r[7], "last_seen": r[8], "created_at": r[9], "updated_at": r[10],
    }


def save_knowledge_entry(conn, container: str, pattern_hash: str,
                         sample_log_line: str, classification: str,
                         user_explanation: str = None) -> int:
    """Create or update a knowledge entry. If pattern_hash exists for the container,
    updates times_seen and last_seen. Returns the entry ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        "SELECT id, times_seen FROM container_knowledge WHERE container = ? AND pattern_hash = ?",
        (container, pattern_hash),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE container_knowledge SET classification = ?, user_explanation = ?, "
            "sample_log_line = ?, times_seen = ?, last_seen = ?, updated_at = ? WHERE id = ?",
            (classification, user_explanation, sample_log_line,
             existing[1] + 1, now, now, existing[0]),
        )
        conn.commit()
        return existing[0]
    else:
        cursor = conn.execute(
            "INSERT INTO container_knowledge "
            "(container, pattern_hash, sample_log_line, classification, user_explanation, "
            "times_seen, first_seen, last_seen, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (container, pattern_hash, sample_log_line, classification, user_explanation,
             now, now, now, now),
        )
        conn.commit()
        return cursor.lastrowid


def update_knowledge_entry(conn, entry_id: int, classification: str = None,
                           user_explanation: str = None) -> bool:
    """Update classification and/or explanation for a knowledge entry."""
    entry = get_knowledge_entry(conn, entry_id)
    if not entry:
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    new_class = classification if classification is not None else entry["classification"]
    new_expl = user_explanation if user_explanation is not None else entry["user_explanation"]
    conn.execute(
        "UPDATE container_knowledge SET classification = ?, user_explanation = ?, updated_at = ? WHERE id = ?",
        (new_class, new_expl, now, entry_id),
    )
    conn.commit()
    return True


def delete_knowledge_entry(conn, entry_id: int) -> bool:
    """Delete a knowledge entry. Returns True if deleted."""
    cursor = conn.execute("DELETE FROM container_knowledge WHERE id = ?", (entry_id,))
    conn.commit()
    return cursor.rowcount > 0




# ─── Pending Patterns ─────────────────────────────────────────

def track_pending_pattern(conn, container: str, pattern_hash: str,
                          sample_log_line: str, ai_guess: str = None) -> int:
    """Track a pattern occurrence. Creates or increments count. Returns entry ID.
    Skips if pattern already exists in container_knowledge."""
    # Check if already classified
    existing_kb = conn.execute(
        "SELECT id FROM container_knowledge WHERE container = ? AND pattern_hash = ?",
        (container, pattern_hash),
    ).fetchone()
    if existing_kb:
        return -1  # Already classified, skip

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        "SELECT id, occurrence_count FROM pending_patterns WHERE container = ? AND pattern_hash = ?",
        (container, pattern_hash),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE pending_patterns SET occurrence_count = ?, last_seen = ?, "
            "sample_log_line = ? WHERE id = ?",
            (existing[1] + 1, now, sample_log_line, existing[0]),
        )
        conn.commit()
        return existing[0]
    else:
        cursor = conn.execute(
            "INSERT INTO pending_patterns "
            "(container, pattern_hash, sample_log_line, occurrence_count, "
            "first_seen, last_seen, ai_guess, surfaced) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 0)",
            (container, pattern_hash, sample_log_line, now, now, ai_guess),
        )
        conn.commit()
        return cursor.lastrowid


def get_pending_patterns(conn, container: str = None, surfaced_only: bool = False,
                         min_occurrences: int = 1) -> list[dict]:
    """Get pending patterns, optionally filtered by container and occurrence threshold."""
    query = ("SELECT id, container, pattern_hash, sample_log_line, occurrence_count, "
             "first_seen, last_seen, ai_guess, surfaced FROM pending_patterns WHERE 1=1")
    params = []
    if container:
        query += " AND container = ?"
        params.append(container)
    if surfaced_only:
        query += " AND surfaced = 1"
    if min_occurrences > 1:
        query += " AND occurrence_count >= ?"
        params.append(min_occurrences)
    query += " ORDER BY occurrence_count DESC, last_seen DESC"
    rows = conn.execute(query, params).fetchall()
    return [
        {"id": r[0], "container": r[1], "pattern_hash": r[2], "sample_log_line": r[3],
         "occurrence_count": r[4], "first_seen": r[5], "last_seen": r[6],
         "ai_guess": r[7], "surfaced": bool(r[8])}
        for r in rows
    ]


def surface_pending_patterns(conn, threshold: int = 3) -> int:
    """Mark patterns with occurrence_count >= threshold as surfaced. Returns count surfaced."""
    cursor = conn.execute(
        "UPDATE pending_patterns SET surfaced = 1 WHERE surfaced = 0 AND occurrence_count >= ?",
        (threshold,),
    )
    conn.commit()
    return cursor.rowcount


def classify_pending_pattern(conn, pattern_id: int, classification: str,
                             user_explanation: str = None) -> dict | None:
    """Classify a pending pattern — moves it to container_knowledge and deletes from pending."""
    row = conn.execute(
        "SELECT container, pattern_hash, sample_log_line, occurrence_count, first_seen "
        "FROM pending_patterns WHERE id = ?",
        (pattern_id,),
    ).fetchone()
    if not row:
        return None
    container, phash, sample, count, first_seen = row
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Insert into knowledge base
    cursor = conn.execute(
        "INSERT INTO container_knowledge "
        "(container, pattern_hash, sample_log_line, classification, user_explanation, "
        "times_seen, first_seen, last_seen, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (container, phash, sample, classification, user_explanation,
         count, first_seen, now, now, now),
    )
    kb_id = cursor.lastrowid
    # Remove from pending
    conn.execute("DELETE FROM pending_patterns WHERE id = ?", (pattern_id,))
    conn.commit()
    return {"knowledge_id": kb_id, "container": container, "classification": classification}


def delete_pending_pattern(conn, pattern_id: int) -> bool:
    """Delete a pending pattern (dismiss without classifying)."""
    cursor = conn.execute("DELETE FROM pending_patterns WHERE id = ?", (pattern_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_pending_pattern_counts(conn) -> dict:
    """Get count of surfaced pending patterns per container (for notification badge)."""
    rows = conn.execute(
        "SELECT container, COUNT(*) FROM pending_patterns WHERE surfaced = 1 GROUP BY container"
    ).fetchall()
    return {r[0]: r[1] for r in rows}




# ─── Container Trend Analysis ─────────────────────────────────

def analyze_container_trends(conn, container: str, period_days: int = 7) -> dict:
    """Analyze eval history for a container over the given period.
    Returns trend data and stores it in the DB."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT status, health_score, root_cause_category FROM eval_history "
        "WHERE container = ? AND timestamp >= ? ORDER BY timestamp",
        (container, cutoff),
    ).fetchall()

    if not rows:
        return {"container": container, "eval_count": 0, "message": "No evaluations in period"}

    scores = [r[1] for r in rows if r[1] is not None]
    statuses = [r[0] for r in rows if r[0]]
    root_causes = [r[2] for r in rows if r[2] and r[2] != "none"]

    eval_count = len(rows)
    avg_score = sum(scores) / len(scores) if scores else 0
    min_score = min(scores) if scores else 0
    max_score = max(scores) if scores else 0

    total = len(statuses) or 1
    healthy_pct = round(statuses.count("healthy") / total * 100, 1)
    degraded_pct = round(statuses.count("degraded") / total * 100, 1)
    unhealthy_pct = round((statuses.count("unhealthy") + statuses.count("critical")) / total * 100, 1)

    # Determine score trend (compare first half vs second half)
    if len(scores) >= 4:
        mid = len(scores) // 2
        first_half_avg = sum(scores[:mid]) / mid
        second_half_avg = sum(scores[mid:]) / (len(scores) - mid)
        diff = second_half_avg - first_half_avg
        if diff > 5:
            score_trend = "improving"
        elif diff < -5:
            score_trend = "declining"
        else:
            score_trend = "stable"
    else:
        score_trend = "stable"

    # Count recurring root causes
    cause_counts = {}
    for c in root_causes:
        cause_counts[c] = cause_counts.get(c, 0) + 1
    recurring = {k: v for k, v in cause_counts.items() if v >= 2}

    # Build summary
    summary_parts = []
    summary_parts.append(f"{eval_count} evaluations over {period_days} days.")
    summary_parts.append(f"Average health score: {avg_score:.0f} (range {min_score}-{max_score}).")
    summary_parts.append(f"Status distribution: {healthy_pct}% healthy, {degraded_pct}% degraded, {unhealthy_pct}% unhealthy.")
    summary_parts.append(f"Score trend: {score_trend}.")
    if recurring:
        causes_str = ", ".join(f"{k} ({v}x)" for k, v in sorted(recurring.items(), key=lambda x: -x[1]))
        summary_parts.append(f"Recurring root causes: {causes_str}.")
    analysis_summary = " ".join(summary_parts)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    recurring_json = json.dumps(recurring) if recurring else None
    conn.execute(
        "INSERT INTO container_trend_analysis "
        "(container, analysis_date, period_days, eval_count, avg_health_score, "
        "min_health_score, max_health_score, healthy_pct, degraded_pct, unhealthy_pct, "
        "recurring_root_causes, score_trend, analysis_summary, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (container, now, period_days, eval_count, round(avg_score, 1),
         min_score, max_score, healthy_pct, degraded_pct, unhealthy_pct,
         recurring_json, score_trend, analysis_summary, now),
    )
    conn.commit()

    return {
        "container": container, "period_days": period_days, "eval_count": eval_count,
        "avg_health_score": round(avg_score, 1), "min_health_score": min_score,
        "max_health_score": max_score, "healthy_pct": healthy_pct,
        "degraded_pct": degraded_pct, "unhealthy_pct": unhealthy_pct,
        "recurring_root_causes": recurring, "score_trend": score_trend,
        "analysis_summary": analysis_summary,
    }


def get_trend_analysis(conn, container: str, limit: int = 10) -> list[dict]:
    """Get recent trend analyses for a container."""
    rows = conn.execute(
        "SELECT id, container, analysis_date, period_days, eval_count, "
        "avg_health_score, min_health_score, max_health_score, "
        "healthy_pct, degraded_pct, unhealthy_pct, "
        "recurring_root_causes, score_trend, analysis_summary "
        "FROM container_trend_analysis WHERE container = ? ORDER BY id DESC LIMIT ?",
        (container, limit),
    ).fetchall()
    return [
        {"id": r[0], "container": r[1], "analysis_date": r[2], "period_days": r[3],
         "eval_count": r[4], "avg_health_score": r[5], "min_health_score": r[6],
         "max_health_score": r[7], "healthy_pct": r[8], "degraded_pct": r[9],
         "unhealthy_pct": r[10],
         "recurring_root_causes": json.loads(r[11]) if r[11] else {},
         "score_trend": r[12], "analysis_summary": r[13]}
        for r in rows
    ]


def prune_trend_analysis(conn, retention_days: int = 90) -> int:
    """Delete old trend analyses."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("DELETE FROM container_trend_analysis WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount



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
