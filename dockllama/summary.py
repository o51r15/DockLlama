"""The daily summary (plan 3.5, 8.9, phase 10).

Once a day at the chosen local time: what happened in the last 24 hours and what still needs
attention. The AI writes it from the stored verdicts as compact rows (never logs), so it stays
small; without AI, or if the AI fails, it's a plain template ("2 problems, 1 restart, 23 fine").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool

from . import ai_queue, ai_servers, db, notify, settings
from .ollama import OllamaClient, OllamaError, RequestSettings, build_generate_request

log = logging.getLogger("dockllama.summary")
SHOWN = notify.SHOWN

PROMPT = """You write a short daily status message about the Docker containers on someone's home server.
The person is not an expert. Use plain words, no jargon, no markdown, no lists of every container.
Lead with whether anything needs them today. Mention what went wrong and got better in the last 24 hours,
and any restarts. If everything is fine, say so in one or two sentences. At most 6 sentences.
Only use the facts below; don't invent anything.

Facts (JSON):
"""


def gather(conn, now: float | None = None) -> dict:
    now = now or time.time()
    since = now - 86400
    states = conn.execute("""SELECT c.id, c.name, m.name AS machine, s.status, s.summary, s.since
                             FROM containers c JOIN machines m ON m.id = c.machine_id
                             LEFT JOIN container_state s ON s.container_id = c.id
                             WHERE c.monitored = 1 AND c.gone = 0""").fetchall()
    counts: dict[str, int] = {}
    problems = []
    for s in states:
        st = s["status"] or "pending"
        counts[st] = counts.get(st, 0) + 1
        if st in ("degraded", "unhealthy", "critical", "unknown"):
            problems.append({"name": s["name"], "status": SHOWN.get(st, st), "since_hours": round((now - s["since"]) / 3600, 1),
                             "summary": (s["summary"] or "")[:200]})
    changes = []
    for e in conn.execute("""SELECT e.at, e.data, c.name FROM events e JOIN containers c ON c.id = e.container_id
                             WHERE e.kind = 'status_change' AND e.at > ? AND c.monitored = 1 AND c.gone = 0
                             ORDER BY e.at""", (since,)):
        d = json.loads(e["data"])
        if d.get("from") is None:
            continue  # first sighting, not a change
        changes.append({"name": e["name"], "from": SHOWN.get(d["from"], d["from"]), "to": SHOWN.get(d["to"], d["to"]),
                        "hours_ago": round((now - e["at"]) / 3600, 1)})
    restarts = [{"name": r["name"], "by": json.loads(r["data"]).get("by"), "hours_ago": round((now - r["at"]) / 3600, 1)}
                for r in conn.execute("""SELECT e.at, e.data, c.name FROM events e JOIN containers c ON c.id = e.container_id
                                         WHERE e.kind = 'restart' AND e.at > ? AND c.monitored = 1
                                         ORDER BY e.at""", (since,))]
    machines = [r["name"] for r in conn.execute("SELECT name FROM machines WHERE enabled = 1 AND last_error IS NOT NULL")]
    return {"watched": len(states), "fine": counts.get("healthy", 0), "stopped": counts.get("stopped", 0),
            "problems_now": problems, "changes_last_24h": changes[-30:], "restarts_last_24h": restarts,
            "machines_unreachable": machines}


def template(data: dict) -> str:
    """The no-AI summary. Also the fallback."""
    p, r = data["problems_now"], data["restarts_last_24h"]
    head = []
    if p:
        head.append(f"{len(p)} need{'s' if len(p) == 1 else ''} attention")
    if r:
        head.append(f"{len(r)} restart{'s' if len(r) != 1 else ''}")
    head.append(f"{data['fine']} fine")
    if data["stopped"]:
        head.append(f"{data['stopped']} stopped")
    lines = [", ".join(head).capitalize() + "."]
    for x in p[:8]:
        lines.append(f"• {x['name']}: {x['status']}. {x['summary']}".strip())
    fixed = [c for c in data["changes_last_24h"] if c["to"] == "Working fine"]
    if fixed:
        names = sorted({c["name"] for c in fixed})
        lines.append("Recovered in the last day: " + ", ".join(names[:8]) + ("." if len(names) <= 8 else " and more."))
    if data["machines_unreachable"]:
        lines.append("Couldn't reach: " + ", ".join(data["machines_unreachable"]) + ".")
    if not p and not r and not fixed:
        lines.append("Nothing needed you in the last 24 hours.")
    return "\n".join(lines)


async def ai_text(data: dict) -> str | None:
    def ctx():
        with db.session() as conn:
            server = ai_servers.connected(conn)
            if server is None or not server["model"]:
                return None
            row = conn.execute("SELECT * FROM models WHERE server_id = ? AND name = ? AND installed = 1",
                               (server["id"], server["model"])).fetchone()
            return (server["address"], ai_servers.model_from_row(row)) if row else None
    found = await run_in_threadpool(ctx)
    if not found:
        return None
    address, model = found
    payload = build_generate_request(model, PROMPT + json.dumps(data, separators=(",", ":")),
                                     settings=RequestSettings(num_predict=400, temperature=0.3))
    if model.thinking != "none":
        payload["options"]["num_predict"] = 1500  # thinking shares the budget
    try:
        async with ai_queue.lock():
            resp = await OllamaClient(address).generate(payload, timeout=120)
    except OllamaError as exc:
        log.warning("Daily summary falls back to the template: %s", exc)
        return None
    text = (resp.get("response") or "").strip()
    if not text or resp.get("done_reason") == "length":
        return None
    return text[:1500]


async def build(now: float | None = None) -> tuple[str, str, bool]:
    """(title, body, written_by_ai)."""
    def g():
        with db.session() as conn:
            return gather(conn, now), settings.get(conn, "general.base_url")
    data, base = await run_in_threadpool(g)
    body = await ai_text(data)
    by_ai = body is not None
    if not by_ai:
        body = template(data)
    n = len(data["problems_now"])
    title = "DockLlama daily summary: " + (f"{n} need{'s' if n == 1 else ''} attention" if n else "all fine")
    return title, body, by_ai


def due(conn, now: float) -> bool:
    if not settings.get(conn, "summary.enabled"):
        return False
    tz = ZoneInfo(settings.get(conn, "general.timezone"))
    local = datetime.fromtimestamp(now, tz)
    hh, mm = (int(x) for x in settings.get(conn, "summary.time").split(":"))
    if (local.hour, local.minute) < (hh, mm):
        return False
    return settings.get(conn, "summary.last_sent") != local.date().isoformat()


async def send(now: float | None = None) -> dict:
    now = now or time.time()
    title, body, by_ai = await build(now)

    def queue():
        with db.session() as conn, db.transaction(conn):
            tz = ZoneInfo(settings.get(conn, "general.timezone"))
            settings.set(conn, "summary.last_sent", datetime.fromtimestamp(now, tz).date().isoformat())
            notify.queue_message(conn, "summary", title, body)
    await run_in_threadpool(queue)
    notify.NOTIFIER.wake()
    return {"title": title, "body": body, "by_ai": by_ai}


class SummaryScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="daily-summary")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)

                def check():
                    with db.session() as conn:
                        return settings.get(conn, "setup.complete") and due(conn, time.time())
                if await run_in_threadpool(check):
                    await send()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Daily summary failed")
                await asyncio.sleep(300)


SUMMARY = SummaryScheduler()
