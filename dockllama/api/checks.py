"""Checks: round progress, Check now, a container's history, and where everything stands."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .. import ai_queue, db
from ..checks import ROUND, SCHEDULER, SHOWN, run_checks

router = APIRouter(prefix="/api", tags=["checks"])


@router.get("/checks/round")
async def round_status():
    return {"running": ROUND.running, "total": ROUND.total, "done": ROUND.done, "current": ROUND.current,
            "phase": ROUND.phase, "note": ROUND.note, "started_at": ROUND.started_at,
            "finished_at": ROUND.finished_at, "next_at": SCHEDULER.next_at, "ai_lock_busy": ai_queue.lock().locked()}


@router.post("/checks/run")
def run_round():
    if ROUND.running:
        return {"ok": True, "already_running": True}
    SCHEDULER.run_now()
    return {"ok": True}


@router.post("/containers/{db_id}/check")
async def check_now(db_id: int):
    def exists():
        with db.session() as conn:
            return conn.execute("SELECT name, gone FROM containers WHERE id = ?", (db_id,)).fetchone()
    row = await run_in_threadpool(exists)
    if row is None:
        raise HTTPException(404, "That container isn't known to DockLlama.")
    waiting = ai_queue.lock().locked()
    results = await run_checks([db_id], force_ai=True, track=False)
    if not results:
        raise HTTPException(503, ROUND.note or "Docker didn't answer.")
    return {**results[0], "shown": SHOWN.get(results[0]["status"]), "waited": waiting}


@router.get("/containers/{db_id}/check/line")
def check_line(db_id: int):
    """Where this container's Check now is in line for the AI, while it waits."""
    pos = ai_queue.position(db_id, ROUND.total - ROUND.done if ROUND.running else 0)
    return {"waiting": pos is not None, **(pos or {"ahead": 0, "what": []})}


def _check_dict(r) -> dict:
    d = dict(r)
    d["warnings"] = json.loads(d["warnings"])
    d["evidence"] = json.loads(d["evidence"])
    d["shown"] = SHOWN.get(d["status"], d["status"])
    return d


@router.get("/containers/{db_id}/checks")
def history(db_id: int, limit: int = 50):
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM checks WHERE container_id = ? ORDER BY at DESC LIMIT ?",
                            (db_id, max(1, min(limit, 500)))).fetchall()
        return {"checks": [_check_dict(r) for r in rows]}


@router.get("/overview")
def overview():
    """Every watched container with where it stands (the dashboard reads this)."""
    with db.session() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.machine_id, c.gone, s.status, s.since, s.summary, s.what_to_do,
                      s.last_checked, s.last_check_id, s.unknown_streak
               FROM containers c LEFT JOIN container_state s ON s.container_id = c.id
               WHERE c.monitored = 1 ORDER BY c.name""").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["status"] = "gone" if d["gone"] else (d["status"] or "pending")
        d["shown"] = SHOWN.get(d["status"], "Not checked yet")
        items.append(d)
    return {"containers": items, "now": time.time()}
