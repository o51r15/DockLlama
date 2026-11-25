"""Settings → Fixing (restart mode and limits) and quiet hours."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import math

from .. import db, settings
from ..restarts import in_quiet_hours
from .setup import per_check, watched_count

HEADROOM = 1.25  # an interval must leave a quarter of a round spare


def round_time(conn) -> tuple[float, bool]:
    """(seconds a full check round takes, measured?). Measured once a round has run; before that,
    estimated from the containers watched and the model's speed (like the setup guide)."""
    measured = settings.get(conn, "checks.last_round_s")
    seconds, _ = per_check(conn)
    estimate = watched_count(conn) * seconds
    if measured:
        return max(measured, 1.0), True
    return estimate, False


def min_interval(round_s: float) -> int:
    """The shortest interval (whole minutes) that fits a round with headroom."""
    return max(1, math.ceil(round_s * HEADROOM / 60))

router = APIRouter(prefix="/api/fixing", tags=["fixing"])

KEYS = {"mode": "fixing.mode", "cooldown_minutes": "fixing.cooldown_minutes", "max_per_hour": "fixing.max_per_hour",
        "quiet_enabled": "quiet.enabled", "quiet_start": "quiet.start", "quiet_end": "quiet.end",
        "interval_minutes": "checks.interval_minutes"}
LABELS = {"mode": "Mode", "cooldown_minutes": "The wait between restarts", "max_per_hour": "Restarts per hour",
          "quiet_enabled": "Quiet hours", "quiet_start": "Quiet hours start", "quiet_end": "Quiet hours end",
          "interval_minutes": "How often to check"}


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} seconds"
    return f"{round(seconds / 60)} minutes"


@router.get("")
def get_fixing():
    with db.session() as conn:
        out = {k: settings.get(conn, v) for k, v in KEYS.items()}
        out["quiet_now"] = in_quiet_hours(conn)
        out["round_seconds"], out["round_measured"] = round_time(conn)
        out["min_interval"] = min_interval(out["round_seconds"])
        out["timezone"] = settings.get(conn, "general.timezone")
        out["allowed"] = [r["name"] for r in conn.execute(
            "SELECT name FROM containers WHERE allow_auto_restart = 1 AND gone = 0 ORDER BY name")]
        return out


class FixingBody(BaseModel):
    mode: str | None = None
    cooldown_minutes: int | None = None
    max_per_hour: int | None = None
    quiet_enabled: bool | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    interval_minutes: int | None = None


@router.put("")
def put_fixing(body: FixingBody):
    changes = body.model_dump(exclude_none=True)
    with db.session() as conn, db.transaction(conn):
        if "interval_minutes" in changes:
            round_s, measured = round_time(conn)
            shortest = min_interval(round_s)
            if changes["interval_minutes"] < shortest:
                how = "takes" if measured else "should take"
                raise HTTPException(400, f"A check round {how} about {_duration(round_s)} here, so checks can't be "
                                         f"closer together than every {shortest} minutes.")
        for key, value in changes.items():
            try:
                settings.set(conn, KEYS[key], value)
            except settings.SettingError as exc:
                raise HTTPException(400, f"{LABELS[key]} {exc}.") from exc
    if "interval_minutes" in changes:
        from ..checks import SCHEDULER
        SCHEDULER.reschedule(changes["interval_minutes"])
    return get_fixing()
