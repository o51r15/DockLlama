"""Alternative AI instructions (plan section 14, Nov 24).

The built-in Default can't be edited. Users can add Prompt 1, 2, 3…, each starting as a copy of
the Default's instructions, and pick exactly one to use for checks. The answer format DockLlama
needs is never part of the editable text: evaluate.system_prompt() adds it at the end.

Model testing and suggestions always use the Default. A custom prompt can be tested from its
own screen; those results are tagged with prompt_id and shown only there.
"""

from __future__ import annotations

import sqlite3
import time

from . import settings
from .evaluate import system_prompt

MAX_CHARS = 20000


class PromptError(ValueError):
    pass


def clean_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) < 50:
        raise PromptError("The instructions are too short to tell the AI how to judge a container.")
    if len(text) > MAX_CHARS:
        raise PromptError(f"The instructions are too long (over {MAX_CHARS:,} characters). Long instructions "
                          "leave less room for the container's logs.")
    return text


def get(conn: sqlite3.Connection, prompt_id: int):
    return conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()


def label(row) -> str:
    if row is None:
        return "Default"
    return f"Prompt {row['number']}" + (f" ({row['name']})" if row["name"] else "")


def active(conn: sqlite3.Connection):
    """The prompt row in use for checks, or None for the Default."""
    pid = settings.get(conn, "ai.active_prompt")
    return get(conn, pid) if pid else None


def active_system(conn: sqlite3.Connection) -> tuple[str, str]:
    """(label, full system prompt) for checks."""
    row = active(conn)
    return label(row), system_prompt(row["text"] if row else None)


def create(conn: sqlite3.Connection, text: str, name: str = "") -> int:
    used = {r["number"] for r in conn.execute("SELECT number FROM prompts")}
    number = next(n for n in range(1, len(used) + 2) if n not in used)  # lowest free: keeps the list 1, 2, 3
    now = time.time()
    return conn.execute("INSERT INTO prompts (number, name, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (number, name.strip()[:60], clean_text(text), now, now)).lastrowid


def update(conn: sqlite3.Connection, prompt_id: int, text: str, name: str) -> None:
    row = get(conn, prompt_id)
    if row is None:
        raise PromptError("That prompt doesn't exist any more.")
    text = clean_text(text)
    rev = row["revision"] + (1 if text != row["text"] else 0)  # renaming doesn't make old tests stale
    conn.execute("UPDATE prompts SET text = ?, name = ?, revision = ?, updated_at = ? WHERE id = ?",
                 (text, name.strip()[:60], rev, time.time(), prompt_id))


def delete(conn: sqlite3.Connection, prompt_id: int) -> None:
    if settings.get(conn, "ai.active_prompt") == prompt_id:
        settings.set(conn, "ai.active_prompt", 0)  # back to the Default
    conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))


def use(conn: sqlite3.Connection, prompt_id: int) -> None:
    if prompt_id and get(conn, prompt_id) is None:
        raise PromptError("That prompt doesn't exist any more.")
    settings.set(conn, "ai.active_prompt", prompt_id)
