"""Automatic restarts (plan section 7).

A container is restarted only when ALL of these hold:
- the global mode is Auto-restart (default: Watch only) and the container allows it;
- it's running (exits, crash loops and out-of-memory kills only alert: Docker's restart policy
  already handles those, and restarting again doesn't help);
- the AI rates it Having problems / Down or failing AND says a restart would help, or Docker's
  health check has failed 3 checks in a row (Docker doesn't restart unhealthy containers);
- it isn't quiet hours, the container isn't in its cooldown, and it's under the limit
  (default 3 restarts in any 60 minutes). At the limit DockLlama stops and asks for help, and
  doesn't restart that container again until it has run fine for 24 hours.
Never restarted: DockLlama itself and the container running its AI server.
Restart groups: if one member qualifies, the whole group restarts, in its order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool

from . import db, machines, notify, settings

log = logging.getLogger("dockllama.restarts")

NO_RESTART_RULES = {"crash_loop", "oom", "oom_loop", "exited_error", "dead", "stopped", "never_started", "paused"}
HEALTH_FAILS_NEEDED = 3
ESCALATION_CLEARS_AFTER = 24 * 3600
FOLLOW_UP = (10, 30)  # seconds after a restart to check again


def in_quiet_hours(conn, now: float | None = None) -> bool:
    if not settings.get(conn, "quiet.enabled"):
        return False
    tz = ZoneInfo(settings.get(conn, "general.timezone"))
    t = datetime.fromtimestamp(now or time.time(), tz)
    minutes = t.hour * 60 + t.minute
    start = _minutes(settings.get(conn, "quiet.start"))
    end = _minutes(settings.get(conn, "quiet.end"))
    if start == end:
        return False
    return start <= minutes < end if start < end else (minutes >= start or minutes < end)


def _minutes(clock: str) -> int:
    h, m = clock.split(":")
    return int(h) * 60 + int(m)


def wants_restart(result: dict, state: dict | None) -> str | None:
    """Why this check result calls for a restart, or None. (Ignores mode, limits, etc.)"""
    if result.get("docker_state") != "running" or result.get("rule") in NO_RESTART_RULES:
        return None
    if result["status"] in ("unhealthy", "critical") and result.get("restart_would_help"):
        return "The AI said a restart would help."
    if result.get("rule") == "health_failing" and (state or {}).get("health_fail_checks", 0) >= HEALTH_FAILS_NEEDED:
        return f"Its health check failed {HEALTH_FAILS_NEEDED} checks in a row."
    return None


def blocked(conn, container_id: int, now: float) -> str | None:
    """Why a wanted restart can't happen now, or None."""
    if settings.get(conn, "fixing.mode") != "auto":
        return "watch_only"
    if in_quiet_hours(conn, now):
        return "quiet_hours"
    row = conn.execute("SELECT allow_auto_restart FROM containers WHERE id = ?", (container_id,)).fetchone()
    if not row or not row["allow_auto_restart"]:
        return "not_allowed"
    state = conn.execute("SELECT escalated_at FROM container_state WHERE container_id = ?", (container_id,)).fetchone()
    if state and state["escalated_at"]:
        return "escalated"
    last = conn.execute("SELECT MAX(at) t FROM events WHERE container_id = ? AND kind = 'restart'", (container_id,)).fetchone()["t"]
    if last and now - last < settings.get(conn, "fixing.cooldown_minutes") * 60:
        return "cooldown"
    recent = conn.execute("SELECT COUNT(*) n FROM events WHERE container_id = ? AND kind = 'restart' AND at > ?",
                          (container_id, now - 3600)).fetchone()["n"]
    if recent >= settings.get(conn, "fixing.max_per_hour"):
        return "limit"
    return None


def clear_escalations(conn, now: float) -> None:
    """The limit resets once the container has run fine for 24 hours."""
    conn.execute("UPDATE container_state SET escalated_at = NULL WHERE escalated_at IS NOT NULL AND status = 'healthy' "
                 "AND since >= escalated_at AND since < ?", (now - ESCALATION_CLEARS_AFTER,))


def group_for(conn, container_id: int) -> list[dict]:
    row = conn.execute("SELECT * FROM containers WHERE id = ?", (container_id,)).fetchone()
    if not row["restart_group"]:
        return [dict(row)]
    return [dict(r) for r in conn.execute(
        "SELECT * FROM containers WHERE machine_id = ? AND restart_group = ? AND gone = 0 ORDER BY restart_order, name",
        (row["machine_id"], row["restart_group"]))]


def plan(results: list[dict], now: float | None = None) -> list[dict]:
    """Decide restarts for a finished round. Returns [{trigger, reason, members}]; records escalations."""
    now = now or time.time()
    out, taken = [], set()
    with db.session() as conn, db.transaction(conn):
        clear_escalations(conn, now)
        for r in results:
            state = conn.execute("SELECT * FROM container_state WHERE container_id = ?", (r["id"],)).fetchone()
            reason = wants_restart(r, dict(state) if state else None)
            if reason is None or r["id"] in taken:
                continue
            why_not = blocked(conn, r["id"], now)
            if why_not == "limit":
                conn.execute("UPDATE container_state SET escalated_at = ? WHERE container_id = ?", (now, r["id"]))
                n = settings.get(conn, "fixing.max_per_hour")
                notify.queue_message(conn, "problems", f"{r['name']} needs you",
                                     f"I've restarted {r['name']} {n} times in the last hour and it's still failing. "
                                     "It needs you. I won't restart it again until it has run fine for a day.", r["id"])
                conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'escalated', ?)",
                             (now, r["id"], json.dumps({"reason": reason})))
                continue
            if why_not:
                continue
            members = group_for(conn, r["id"])
            taken.update(m["id"] for m in members)
            out.append({"trigger": r, "reason": reason, "members": members})
    return out


async def carry_out(job: dict, run_checks) -> None:
    """Restart a container (or its group), then check at 10 s and 30 s and say whether it worked."""
    trigger = job["trigger"]
    docker = machines.get_machine(trigger.get("machine_id", 1))
    try:
        infos = {c.name: c for c in await docker.alist_containers()}
    except machines.MachineError as exc:
        log.warning("Restart of %s skipped: %s", trigger["name"], exc)
        return
    done, skipped = [], []
    for m in job["members"]:
        info = infos.get(m["name"])
        if info is None or info.is_self or info.is_ollama:
            skipped.append(m["name"])  # never DockLlama itself or the AI server
            continue
        try:
            await docker.arestart(m["name"])
            done.append(m)
        except machines.MachineError as exc:
            skipped.append(f"{m['name']} ({exc})")

    def record():
        with db.session() as conn, db.transaction(conn):
            group = [m["name"] for m in done]
            for m in done:
                conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'restart', ?)",
                             (time.time(), m["id"], json.dumps({"by": "dockllama", "reason": job["reason"],
                                                                "trigger": trigger["name"], "group": group})))
    await run_in_threadpool(record)
    if not done:
        return
    names = ", ".join(m["name"] for m in done)
    status = None
    for delay in FOLLOW_UP:
        await asyncio.sleep(delay - (FOLLOW_UP[0] if delay != FOLLOW_UP[0] else 0))
        results = await run_checks([trigger["id"]], force_ai=True, track=False)
        status = results[0]["status"] if results else None
    worked = status == "healthy"
    title = f"Restarted {trigger['name']}" + (": it's working again" if worked else ": still not right")
    body = f"{job['reason']} I restarted {names}. " + (
        "It's working fine now." if worked else f"After 30 seconds it's still: {notify.SHOWN.get(status, status)}.")

    def tell():
        with db.session() as conn:
            notify.queue_message(conn, "restarts", title, body, trigger["id"])
    await run_in_threadpool(tell)
    notify.NOTIFIER.wake()


_tasks: set[asyncio.Task] = set()


async def after_round(results: list[dict], run_checks) -> None:
    jobs = await run_in_threadpool(plan, results)
    for job in jobs:
        task = asyncio.create_task(carry_out(job, run_checks))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
