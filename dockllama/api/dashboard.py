"""The dashboard and the container page."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .. import ai_servers, db, machines, settings
from ..checks import ROUND, SCHEDULER, SHOWN, run_checks
from ..redact import redact

router = APIRouter(prefix="/api", tags=["dashboard"])

PROBLEMS = ("critical", "unhealthy", "degraded", "unknown", "gone")
ORDER = {"critical": 0, "unhealthy": 1, "gone": 2, "unknown": 3, "degraded": 4, "pending": 5, "healthy": 6, "stopped": 7}


@router.get("/dashboard")
def dashboard():
    with db.session() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.gone, c.machine_id, m.name AS machine, s.status, s.since, s.summary,
                      s.what_to_do, s.last_checked, s.unknown_streak
               FROM containers c JOIN machines m ON m.id = c.machine_id
               LEFT JOIN container_state s ON s.container_id = c.id
               WHERE c.monitored = 1""").fetchall()
        server = ai_servers.connected(conn)
        machine_error = conn.execute("SELECT last_error FROM machines WHERE id = 1").fetchone()["last_error"]
        interval = settings.get(conn, "checks.interval_minutes")
        last_checked = conn.execute("SELECT MAX(last_checked) t FROM container_state").fetchone()["t"]
    items = []
    for r in rows:
        d = dict(r)
        d["status"] = "gone" if d["gone"] else (d["status"] or "pending")
        d["shown"] = SHOWN.get(d["status"], "Not checked yet")
        items.append(d)
    items.sort(key=lambda d: (ORDER.get(d["status"], 9), d["name"]))
    counts: dict[str, int] = {}
    for d in items:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return {
        "containers": items, "counts": counts,
        "problems": sum(counts.get(s, 0) for s in PROBLEMS if s != "degraded"),
        "attention": sum(counts.get(s, 0) for s in PROBLEMS),
        "round": {"running": ROUND.running, "done": ROUND.done, "total": ROUND.total, "current": ROUND.current,
                  "note": ROUND.note, "finished_at": ROUND.finished_at},
        "last_checked": last_checked, "next_at": SCHEDULER.next_at, "interval_minutes": interval,
        "ai": {"connected": server is not None, "model": server["model"] if server else None},
        "machine_error": machine_error, "now": time.time(),
    }


def _container(conn, db_id: int):
    row = conn.execute("SELECT c.*, m.name AS machine FROM containers c JOIN machines m ON m.id = c.machine_id "
                       "WHERE c.id = ?", (db_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "That container isn't known to DockLlama.")
    return row


@router.get("/containers/{db_id}/detail")
async def detail(db_id: int):
    def read():
        with db.session() as conn:
            row = _container(conn, db_id)
            state = conn.execute("SELECT * FROM container_state WHERE container_id = ?", (db_id,)).fetchone()
            last = conn.execute("SELECT * FROM checks WHERE id = ?", (state["last_check_id"],)).fetchone() if state else None
            hist = conn.execute("SELECT id, at, status, source, summary, what_to_do, model, duration_s FROM checks "
                                "WHERE container_id = ? ORDER BY at DESC LIMIT 100", (db_id,)).fetchall()
            stats = conn.execute("SELECT at, cpu, mem FROM stats WHERE container_id = ? AND at > ? ORDER BY at",
                                 (db_id, time.time() - 86400)).fetchall()
            restarts = conn.execute("SELECT at, data FROM events WHERE container_id = ? AND kind = 'restart' "
                                    "ORDER BY at DESC LIMIT 10", (db_id,)).fetchall()
            return row, state, last, hist, stats, restarts
    row, state, last, hist, stats, restarts = await run_in_threadpool(read)
    info = None
    try:
        for c in await machines.get_machine(row["machine_id"]).alist_containers():
            if c.name == row["name"]:
                info = c.to_dict()
    except machines.MachineError:
        pass
    latest = None
    if last is not None:
        latest = dict(last)
        latest["evidence"] = json.loads(latest["evidence"])
        latest["warnings"] = json.loads(latest["warnings"])
        latest["shown"] = SHOWN.get(latest["status"], latest["status"])
    status = "gone" if row["gone"] else (state["status"] if state else "pending")
    can_restart, why_not = True, ""
    if info is None:
        can_restart, why_not = False, "Docker doesn't have this container any more."
    elif info["is_self"]:
        can_restart, why_not = False, "This is DockLlama itself."
    return {
        "id": db_id, "name": row["name"], "machine": row["machine"], "monitored": bool(row["monitored"]),
        "notes_for_ai": row["notes_for_ai"], "allow_auto_restart": bool(row["allow_auto_restart"]),
        "restart_group": row["restart_group"], "restart_order": row["restart_order"],
        "status": status, "shown": SHOWN.get(status, "Not checked yet"),
        "since": state["since"] if state else None, "summary": state["summary"] if state else "",
        "what_to_do": state["what_to_do"] if state else "", "last_checked": state["last_checked"] if state else None,
        "latest": latest, "info": info,
        "history": [dict(h) | {"shown": SHOWN.get(h["status"], h["status"])} for h in hist],
        "stats": [dict(s) for s in stats],
        "restarts": [{"at": r["at"], **json.loads(r["data"])} for r in restarts],
        "can_restart": can_restart, "why_not_restart": why_not, "now": time.time(),
    }


@router.get("/containers/{db_id}/logs")
async def recent_logs(db_id: int, lines: int = 100):
    def read():
        with db.session() as conn:
            return _container(conn, db_id)
    row = await run_in_threadpool(read)
    try:
        got = await machines.get_machine(row["machine_id"]).alogs(row["name"], None, max(10, min(lines, 500)))
    except machines.MachineError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"lines": [{"at": ts, "text": redact(text[:2000])} for ts, text in got]}


@router.post("/containers/{db_id}/restart")
async def restart(db_id: int):
    def read():
        with db.session() as conn:
            return _container(conn, db_id)
    row = await run_in_threadpool(read)
    docker = machines.get_machine(row["machine_id"])
    try:
        infos = {c.name: c for c in await docker.alist_containers()}
    except machines.MachineError as exc:
        raise HTTPException(503, str(exc)) from exc
    info = infos.get(row["name"])
    if info is None:
        raise HTTPException(409, "Docker doesn't have this container any more.")
    if info.is_self:
        raise HTTPException(409, "DockLlama can't restart itself. Use docker restart dockllama on the server.")
    try:
        await docker.arestart(row["name"])
    except machines.MachineError as exc:
        raise HTTPException(503, f"The restart didn't work: {exc}") from exc

    def record():
        with db.session() as conn:
            conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'restart', ?)",
                         (time.time(), db_id, json.dumps({"by": "you", "reason": "Restarted from the container page."})))
    await run_in_threadpool(record)

    async def follow_up():  # plan section 7: check again shortly after a restart
        await asyncio.sleep(10)
        try:
            await run_checks([db_id], force_ai=False, track=False)
        except Exception:  # noqa: BLE001 - best effort
            pass
    asyncio.create_task(follow_up())
    return {"ok": True}
