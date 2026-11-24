"""AI servers in the database: which one is connected, recognising old ones, their models.

Plan section 8C: one connected server at a time; every server's data is kept; only the
connected server's data is shown. A server is recognised by its model inventory (not its
IP), with the address as a fallback, and the user confirms any match.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .ollama import ModelDetails, fingerprint_overlap

MATCH_THRESHOLD = 0.5


@dataclass
class Match:
    server_id: int
    name: str
    last_address: str
    how: str  # "address" | "fingerprint"
    tested_models: int


def connected(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM ai_servers WHERE connected = 1").fetchone()


def find_match(conn: sqlite3.Connection, address: str, fingerprint: list[str]) -> Match | None:
    """Which stored server (if any) this is. Inventory first; address only when inventory can't tell."""
    rows = conn.execute("SELECT * FROM ai_servers").fetchall()
    best, best_overlap = None, 0.0
    for row in rows:
        overlap = fingerprint_overlap(json.loads(row["fingerprint"]), fingerprint)
        if overlap > best_overlap:
            best, best_overlap = row, overlap
    how = "fingerprint"
    if best is None or best_overlap < MATCH_THRESHOLD:
        best = next((r for r in rows if r["address"] == address), None)
        how = "address"
        if best is None:
            return None
    tested = 0  # filled in once model tests exist (phase 3b)
    return Match(best["id"], best["name"], best["address"], how, tested)


LOCAL_NAMES = {"host.docker.internal", "172.17.0.1", "localhost", "127.0.0.1", "ollama"}


def default_name(address: str) -> str:
    host = address.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]")
    return "Ollama on this machine" if host in LOCAL_NAMES else host


def connect(conn: sqlite3.Connection, address: str, version: str, fingerprint: list[str],
            reuse_id: int | None) -> int:
    """Make `reuse_id` (or a new server) the connected one. Caller holds a transaction."""
    now = time.time()
    conn.execute("UPDATE ai_servers SET connected = 0")
    if reuse_id is not None:
        conn.execute(
            "UPDATE ai_servers SET address = ?, version = ?, fingerprint = ?, connected = 1, last_seen = ? WHERE id = ?",
            (address, version, json.dumps(fingerprint), now, reuse_id),
        )
        return reuse_id
    cur = conn.execute(
        "INSERT INTO ai_servers (name, address, version, fingerprint, connected, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (default_name(address), address, version, json.dumps(fingerprint), now, now),
    )
    return cur.lastrowid


def save_models(conn: sqlite3.Connection, server_id: int, models: list[ModelDetails], fingerprint: list[str]) -> None:
    now = time.time()
    names = [m.name for m in models]
    for m in models:
        conn.execute(
            """INSERT INTO models (server_id, name, digest, modified_at, size, family, parameter_size,
                   capabilities, max_context, thinking, think_levels, is_cloud, installed, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(server_id, name) DO UPDATE SET digest = excluded.digest,
                   modified_at = excluded.modified_at, size = excluded.size, family = excluded.family,
                   parameter_size = excluded.parameter_size, capabilities = excluded.capabilities,
                   max_context = excluded.max_context, thinking = excluded.thinking,
                   think_levels = excluded.think_levels, is_cloud = excluded.is_cloud, installed = 1""",
            (server_id, m.name, m.digest, m.modified_at, m.size, m.family, m.parameter_size,
             json.dumps(m.capabilities), m.max_context, m.thinking, json.dumps(m.think_levels),
             int(m.is_cloud), now),
        )
    placeholders = ",".join("?" * len(names)) or "''"
    conn.execute(
        f"UPDATE models SET installed = 0 WHERE server_id = ? AND name NOT IN ({placeholders})",
        (server_id, *names),
    )
    conn.execute(
        "UPDATE ai_servers SET fingerprint = ?, last_seen = ? WHERE id = ?",
        (json.dumps(fingerprint), now, server_id),
    )


def model_from_row(row) -> ModelDetails:
    return ModelDetails(
        name=row["name"], digest=row["digest"], modified_at=row["modified_at"], size=row["size"],
        family=row["family"], parameter_size=row["parameter_size"],
        capabilities=json.loads(row["capabilities"]), max_context=row["max_context"],
        thinking=row["thinking"], think_levels=json.loads(row["think_levels"]), is_cloud=bool(row["is_cloud"]),
    )


def installed_models(conn: sqlite3.Connection, server_id: int) -> list[ModelDetails]:
    rows = conn.execute(
        "SELECT * FROM models WHERE server_id = ? AND installed = 1 ORDER BY name", (server_id,)
    ).fetchall()
    return [model_from_row(r) for r in rows]
