"""SQLite access and schema migrations.

One file database in the data volume, WAL mode. Each request opens its own
short-lived connection (sqlite3 connections are cheap and not thread-safe to
share). Migrations are numbered functions applied in order on startup; before
applying any to an existing database, a copy is saved in data/backups/.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from . import paths

KEEP_BACKUPS = 5


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or paths.DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    """Connection for one unit of work; closed afterwards."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- migrations -------------------------------------------------------------


def _run(conn: sqlite3.Connection, script: str) -> None:
    """Execute ';'-separated DDL inside the current transaction.

    Not executescript(): that issues an implicit COMMIT first, which would
    break the one-transaction-per-migration guarantee.
    """
    # Split with SQLite's own parser (sqlite3.complete_statement), so semicolons inside
    # comments or strings don't break a statement in two.
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""
    if buffer.strip():
        if not sqlite3.complete_statement(buffer + "\n;"):
            raise ValueError(f"Incomplete SQL in migration: {buffer.strip()[:80]}")
        conn.execute(buffer)


def _m001_initial(conn: sqlite3.Connection) -> None:
    _run(
        conn,
        """
        CREATE TABLE settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,          -- JSON
            updated_at REAL NOT NULL
        );
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,       -- sha256 of the cookie value
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            user_agent TEXT
        );
        CREATE INDEX sessions_expires ON sessions(expires_at);
        """
    )


def _m002_machines_containers(conn: sqlite3.Connection) -> None:
    _run(
        conn,
        """
        CREATE TABLE machines (
            id             INTEGER PRIMARY KEY,
            name           TEXT NOT NULL,
            kind           TEXT NOT NULL CHECK (kind IN ('local', 'remote')),
            url            TEXT NOT NULL,
            token          TEXT,
            allows_restart INTEGER NOT NULL DEFAULT 1,
            enabled        INTEGER NOT NULL DEFAULT 1,
            last_seen      REAL,
            last_error     TEXT
        );
        INSERT INTO machines (id, name, kind, url) VALUES (1, 'This machine', 'local', 'unix:///var/run/docker.sock');
        CREATE TABLE containers (
            id                 INTEGER PRIMARY KEY,
            machine_id         INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
            name               TEXT NOT NULL,
            monitored          INTEGER NOT NULL DEFAULT 0,
            notes_for_ai       TEXT NOT NULL DEFAULT '',
            restart_group      TEXT NOT NULL DEFAULT '',
            model_override     TEXT,
            allow_auto_restart INTEGER NOT NULL DEFAULT 0,
            ignore_patterns    TEXT NOT NULL DEFAULT '[]',
            known_patterns     TEXT NOT NULL DEFAULT '[]',
            first_seen         REAL NOT NULL,
            last_seen          REAL NOT NULL,
            gone               INTEGER NOT NULL DEFAULT 0,
            last_log_ts        REAL,
            UNIQUE (machine_id, name)
        );
        CREATE INDEX containers_machine ON containers(machine_id)
        """,
    )


def _m003_ai(conn: sqlite3.Connection) -> None:
    _run(
        conn,
        """
        CREATE TABLE ai_servers (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            address     TEXT NOT NULL,
            model       TEXT,                       -- the model the user chose for checks
            hardware    TEXT NOT NULL DEFAULT 'unknown',
            version     TEXT,
            fingerprint TEXT NOT NULL DEFAULT '[]', -- JSON list, see ollama.inventory_fingerprint
            connected   INTEGER NOT NULL DEFAULT 0, -- exactly one row is 1 once set up
            created_at  REAL NOT NULL,
            last_seen   REAL
        );
        CREATE TABLE models (
            server_id    INTEGER NOT NULL REFERENCES ai_servers(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            digest       TEXT NOT NULL DEFAULT '',
            modified_at  TEXT NOT NULL DEFAULT '',
            size         INTEGER NOT NULL DEFAULT 0,
            family       TEXT NOT NULL DEFAULT '',
            parameter_size TEXT NOT NULL DEFAULT '',
            capabilities TEXT NOT NULL DEFAULT '[]',
            max_context  INTEGER,
            thinking     TEXT NOT NULL DEFAULT 'none',
            think_levels TEXT NOT NULL DEFAULT '[]',
            is_cloud     INTEGER NOT NULL DEFAULT 0,
            installed    INTEGER NOT NULL DEFAULT 1,
            first_seen   REAL NOT NULL,
            PRIMARY KEY (server_id, name)
        );
        CREATE TABLE model_settings (
            server_id INTEGER NOT NULL REFERENCES ai_servers(id) ON DELETE CASCADE,
            name      TEXT NOT NULL,
            settings  TEXT NOT NULL,                -- JSON; missing keys = recommended
            PRIMARY KEY (server_id, name)
        )
        """,
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m001_initial,
    _m002_machines_containers,
    _m003_ai,
]

LATEST = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row["version"] if row else 0


def _backup(conn: sqlite3.Connection, version: int, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"dockllama-v{version}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    old = sorted(backup_dir.glob("dockllama-v*.db"), key=lambda p: p.stat().st_mtime)
    for stale in old[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    return target


def migrate(db_path: Path | None = None, backup_dir: Path | None = None) -> int:
    """Bring the database to the latest schema. Returns the resulting version."""
    db_path = db_path or paths.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with session(db_path) as conn:
        version = current_version(conn)
        if version > LATEST:
            raise RuntimeError(
                f"Database schema v{version} is newer than this DockLlama (v{LATEST}). "
                "Update DockLlama, or restore a backup from data/backups/."
            )
        if version == LATEST:
            return version
        if version > 0:
            _backup(conn, version, backup_dir or db_path.parent / "backups")
        for number in range(version + 1, LATEST + 1):
            with transaction(conn):
                MIGRATIONS[number - 1](conn)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
        return LATEST
