"""Global settings: a typed key/value store in the `settings` table.

Every setting is declared once in REGISTRY with its default and a validator.
Reading an unset key returns the default; writing an unknown key or an
invalid value raises SettingError. Values are stored as JSON.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    default: Any
    validate: Callable[[Any], Any]
    exportable: bool = True  # False for secrets (password hash) — never exported


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise SettingError("must be true or false")


def _opt_str(v: Any) -> str | None:
    if v is None or isinstance(v, str):
        return v
    raise SettingError("must be text")


def _timezone(v: Any) -> str:
    if not isinstance(v, str) or not v:
        raise SettingError("must be a timezone name like America/New_York")
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingError(f"unknown timezone {v!r}") from exc
    return v


def _clock(v: Any) -> str:
    if isinstance(v, str):
        parts = v.split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
    raise SettingError("must be a time like 01:00")


REGISTRY: dict[str, Setting] = {
    "auth.password_hash": Setting(None, _opt_str, exportable=False),
    "auth.login_required": Setting(True, _bool),
    "general.timezone": Setting("UTC", _timezone),
    "setup.complete": Setting(False, _bool),
    "ai.auto_test_new": Setting(True, _bool),
    # Start of quiet hours, local time; "Run tonight" starts model tests then. Phase 8 adds the rest.
    "quiet.start": Setting("01:00", _clock),
}


def _spec(key: str) -> Setting:
    try:
        return REGISTRY[key]
    except KeyError:
        raise SettingError(f"unknown setting {key!r}") from None


def get(conn: sqlite3.Connection, key: str) -> Any:
    spec = _spec(key)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return spec.default if row is None else json.loads(row["value"])


def set(conn: sqlite3.Connection, key: str, value: Any) -> Any:  # noqa: A001 - module API
    spec = _spec(key)
    clean = spec.validate(value)
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(clean), time.time()),
    )
    return clean


def get_all(conn: sqlite3.Connection, exportable_only: bool = True) -> dict[str, Any]:
    return {
        key: get(conn, key)
        for key, spec in REGISTRY.items()
        if spec.exportable or not exportable_only
    }
