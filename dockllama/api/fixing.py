"""Settings → Fixing (restart mode and limits) and quiet hours."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, settings
from ..restarts import in_quiet_hours

router = APIRouter(prefix="/api/fixing", tags=["fixing"])

KEYS = {"mode": "fixing.mode", "cooldown_minutes": "fixing.cooldown_minutes", "max_per_hour": "fixing.max_per_hour",
        "quiet_enabled": "quiet.enabled", "quiet_start": "quiet.start", "quiet_end": "quiet.end",
        "interval_minutes": "checks.interval_minutes"}
LABELS = {"mode": "Mode", "cooldown_minutes": "The wait between restarts", "max_per_hour": "Restarts per hour",
          "quiet_enabled": "Quiet hours", "quiet_start": "Quiet hours start", "quiet_end": "Quiet hours end",
          "interval_minutes": "How often to check"}


@router.get("")
def get_fixing():
    with db.session() as conn:
        out = {k: settings.get(conn, v) for k, v in KEYS.items()}
        out["quiet_now"] = in_quiet_hours(conn)
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
        for key, value in changes.items():
            try:
                settings.set(conn, KEYS[key], value)
            except settings.SettingError as exc:
                raise HTTPException(400, f"{LABELS[key]} {exc}.") from exc
    if "interval_minutes" in changes:
        from ..checks import SCHEDULER
        SCHEDULER.reschedule(changes["interval_minutes"])
    return get_fixing()
