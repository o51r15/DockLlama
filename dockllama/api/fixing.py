"""Settings → Fixing (restart mode and limits) and quiet hours."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import asyncio
import math

from .. import db, settings
from ..restarts import in_quiet_hours
from .setup import per_check, watched_count
from starlette.concurrency import run_in_threadpool

from .. import machines

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
        out["coverage"] = coverage_counts(conn)
        return out


def coverage_counts(conn) -> dict:
    """How many watched containers Auto-restart actually covers, and why the rest aren't."""
    rows = conn.execute("SELECT c.allow_auto_restart, m.allows_restart FROM containers c JOIN machines m "
                        "ON m.id = c.machine_id WHERE c.monitored = 1 AND c.gone = 0 AND m.enabled = 1").fetchall()
    return {"watched": len(rows),
            "covered": sum(1 for r in rows if r["allow_auto_restart"] and r["allows_restart"]),
            "not_allowed": sum(1 for r in rows if not r["allow_auto_restart"] and r["allows_restart"]),
            "machine_watch_only": sum(1 for r in rows if not r["allows_restart"])}


@router.get("/coverage")
async def coverage():
    """Every watched container, grouped by machine, for choosing which ones Auto-restart covers."""
    def read():
        with db.session() as conn:
            ms = [dict(r) for r in conn.execute("SELECT id, name, kind, allows_restart FROM machines "
                                                "WHERE enabled = 1 ORDER BY kind = 'local' DESC, name")]
            cs = conn.execute("SELECT id, machine_id, name, allow_auto_restart FROM containers "
                              "WHERE monitored = 1 AND gone = 0 ORDER BY name COLLATE NOCASE").fetchall()
            return ms, cs, coverage_counts(conn)
    ms, cs, counts = await run_in_threadpool(read)

    async def never_for(m) -> dict[str, str] | None:
        """DockLlama itself and the container running the AI are never restarted. None when the
        machine can't be reached quickly; the list still shows, without those marks."""
        try:
            infos = await asyncio.wait_for(machines.get_machine(m["id"]).alist_containers(), timeout=3)
        except (machines.MachineError, asyncio.TimeoutError):
            return None
        return {i.name: ("DockLlama itself" if i.is_self else "runs your AI") for i in infos if i.is_self or i.is_ollama}

    nevers = await asyncio.gather(*(never_for(m) for m in ms))  # all machines at once
    out = []
    for m, found in zip(ms, nevers):
        never, reachable = found or {}, found is not None
        mine = [{"id": c["id"], "name": c["name"], "allowed": bool(c["allow_auto_restart"]),
                 "never": never.get(c["name"])} for c in cs if c["machine_id"] == m["id"]]
        if mine:
            out.append({**m, "allows_restart": bool(m["allows_restart"]), "reachable": reachable, "containers": mine})
    return {"machines": out, "counts": counts}


class CoverageBody(BaseModel):
    allowed_ids: list[int]          # watched containers Auto-restart may restart; every other one is turned off
    turn_on: bool = False           # also switch the mode to Auto-restart, in the same step


@router.put("/coverage")
def set_coverage(body: CoverageBody):
    allowed = set(body.allowed_ids)
    with db.session() as conn, db.transaction(conn):
        watched = [r["id"] for r in conn.execute("SELECT id FROM containers WHERE monitored = 1 AND gone = 0")]
        for cid in watched:
            conn.execute("UPDATE containers SET allow_auto_restart = ? WHERE id = ?", (int(cid in allowed), cid))
        if body.turn_on:
            settings.set(conn, "fixing.mode", "auto")
    return get_fixing()


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
