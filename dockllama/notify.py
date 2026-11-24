"""Notifications (plan 3.2 step 5, 3.5, 6.8, phase 7).

The user picks a service and fills in one or two fields; DockLlama builds the Apprise URL.
What gets sent:
- a problem starts (Having problems / Down or failing; "Needs a look" only if they asked);
- it clears ("recovered"), when the problem was alerted;
- a "still broken" reminder every N hours while it lasts (default 6; 0 = never);
- DockLlama can't check containers (3 rounds in a row);
- restarts (phase 8) and the daily summary (phase 10) use the same targets.
Several changes found in one pass are sent as one message, so a bad moment doesn't send ten.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from urllib.parse import quote, urlparse

from starlette.concurrency import run_in_threadpool

from . import db, settings

log = logging.getLogger("dockllama.notify")

SERVICES = ("ntfy", "discord", "telegram", "gotify", "pushover", "email", "apprise")
EVENT_KINDS = ("problems", "recoveries", "reminders", "needs_look", "restarts", "summary", "checking")
DEFAULT_EVENTS = {"problems": True, "recoveries": True, "reminders": True, "needs_look": False,
                  "restarts": True, "summary": True, "checking": True}
ALERTING = ("unhealthy", "critical", "gone")
SHOWN = {"healthy": "Working fine", "degraded": "Needs a look", "unhealthy": "Having problems",
         "critical": "Down or failing", "stopped": "Stopped", "unknown": "Couldn't check", "gone": "No longer exists"}
COMBINE_OVER = 3  # more than this many alerts in one pass become one message
UNKNOWN_ALERT_STREAK = 3


class NotifyError(ValueError):
    """A problem with what the user typed, worded for them."""


def new_ntfy_topic() -> str:
    """Hard to guess: anyone who knows an ntfy.sh topic can read it."""
    return "dockllama-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y").lower()


def _need(fields: dict, key: str, label: str) -> str:
    value = str(fields.get(key) or "").strip()
    if not value:
        raise NotifyError(f"Fill in {label}.")
    return value


def _host(url: str, label: str) -> tuple[str, str]:
    """('ntfys' or 'ntfy'-style secure flag, host[:port][/path]) from what the user typed."""
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    p = urlparse(text)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise NotifyError(f"{label} should look like https://example.com")
    return ("s" if p.scheme == "https" else ""), (p.netloc + p.path.rstrip("/"))


def build_url(service: str, fields: dict) -> str:
    if service == "ntfy":
        topic = _need(fields, "topic", "the topic")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise NotifyError("A topic can only use letters, numbers, - and _.")
        secure, host = _host(fields.get("server") or "https://ntfy.sh", "The ntfy server")
        url = f"ntfy{secure}://{host}/{topic}"
        if (fields.get("token") or "").strip():
            url += "?token=" + quote(fields["token"].strip(), safe="")
        return url
    if service == "discord":
        hook = _need(fields, "webhook", "the webhook URL")
        if not re.match(r"https://(\w+\.)?(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+", hook):
            raise NotifyError("That isn't a Discord webhook URL. In Discord: channel settings → Integrations → Webhooks → Copy Webhook URL.")
        return hook
    if service == "telegram":
        token = _need(fields, "bot_token", "the bot token")
        chat = _need(fields, "chat_id", "the chat ID")
        if not re.fullmatch(r"\d+:[\w-]+", token):
            raise NotifyError("A bot token looks like 123456789:ABCdef… (from @BotFather).")
        if not re.fullmatch(r"-?\d+|@\w+", chat):
            raise NotifyError("The chat ID is a number (from @userinfobot), or @channelname.")
        return f"tgram://{token}/{chat}"
    if service == "gotify":
        secure, host = _host(_need(fields, "server", "the Gotify server"), "The Gotify server")
        return f"gotify{secure}://{host}/{_need(fields, 'app_token', 'the app token')}"
    if service == "pushover":
        return f"pover://{_need(fields, 'user_key', 'your user key')}@{_need(fields, 'app_token', 'the app token')}"
    if service == "email":
        address = _need(fields, "address", "your email address")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            raise NotifyError("That doesn't look like an email address.")
        password = _need(fields, "password", "the app password")
        user, domain = address.split("@", 1)
        url = f"mailtos://{quote(user, safe='')}:{quote(password, safe='')}@{domain}?to={quote(address, safe='@')}"
        if (fields.get("smtp") or "").strip():
            url += "&smtp=" + quote(fields["smtp"].strip(), safe="")
        return url
    if service == "apprise":
        url = _need(fields, "url", "the Apprise URL")
        if "://" not in url:
            raise NotifyError("An Apprise URL looks like service://… (see the Apprise wiki).")
        return url
    raise NotifyError("Pick one of the listed services.")


def subscribe_link(service: str, fields: dict) -> str | None:
    """For ntfy: the link the phone app (or a browser) opens to subscribe."""
    if service != "ntfy":
        return None
    secure, host = _host(fields.get("server") or "https://ntfy.sh", "The ntfy server")
    return f"http{secure}://{host}/{fields.get('topic', '')}"


def qr_svg(text: str) -> str:
    """A standalone SVG (with xmlns, so it works as an <img>). Dark on light scans most reliably."""
    import io

    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, dark="#000000", light="#ffffff", border=2, xmldecl=False)
    return buf.getvalue().decode()


def _valid_url(url: str) -> bool:
    import apprise
    return apprise.Apprise().add(url)


def send_now(url: str, title: str, body: str) -> tuple[bool, str]:
    """Blocking send through Apprise. Returns (ok, problem in plain words)."""
    import apprise
    a = apprise.Apprise()
    if not a.add(url):
        return False, "DockLlama couldn't use those details. Check them and try again."
    try:
        ok = a.notify(title=title, body=body)
    except Exception as exc:  # noqa: BLE001 - any delivery failure is reported, never raised
        return False, f"Sending failed: {exc}"
    return (True, "") if ok else (False, "The service didn't accept the message. Check the details and that it can be reached.")


# --- deciding what to send ------------------------------------------------------------------


def _link(conn, container_id: int | None) -> str:
    base = settings.get(conn, "general.base_url")
    if not base:
        return ""
    return f"\n{base.rstrip('/')}/" + (f"container?id={container_id}" if container_id else "")


def _targets(conn, kind: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM notify_targets WHERE enabled = 1 ORDER BY id"):
        events = {**DEFAULT_EVENTS, **json.loads(r["events"])}
        if events.get(kind):
            out.append(dict(r))
    return out


def plan_messages(conn, now: float | None = None) -> list[dict]:
    """Work out what to send from new status changes, reminders and failing checks; mark it
    handled. Returns [{kind, title, body, container_ids}]. Pure database work, no sending."""
    now = now or time.time()
    reminder_hours = settings.get(conn, "notify.reminder_hours")
    any_needs_look = bool(_targets(conn, "needs_look"))
    problems, recoveries, reminders = [], [], []
    events = conn.execute("SELECT e.*, c.name FROM events e LEFT JOIN containers c ON c.id = e.container_id "
                          "WHERE e.kind = 'status_change' AND e.handled = 0 ORDER BY e.id").fetchall()
    for e in events:
        conn.execute("UPDATE events SET handled = 1 WHERE id = ?", (e["id"],))
        if e["container_id"] is None:
            continue
        d = json.loads(e["data"])
        state = conn.execute("SELECT * FROM container_state WHERE container_id = ?", (e["container_id"],)).fetchone()
        if state is None or state["status"] != d["to"]:
            continue  # it has already moved on; the later event decides
        alerting = d["to"] in ALERTING or (d["to"] == "degraded" and any_needs_look)
        if alerting and not state["alerted"]:
            problems.append({"id": e["container_id"], "name": e["name"], "status": d["to"],
                             "summary": state["summary"], "what_to_do": state["what_to_do"],
                             "needs_look_only": d["to"] == "degraded"})
            conn.execute("UPDATE container_state SET alerted = 1, last_alert_at = ? WHERE container_id = ?",
                         (now, e["container_id"]))
        elif d["to"] in ("healthy", "stopped") and state["alerted"]:
            recoveries.append({"id": e["container_id"], "name": e["name"], "status": d["to"],
                               "was": d.get("from"), "since": state["since"]})
            conn.execute("UPDATE container_state SET alerted = 0 WHERE container_id = ?", (e["container_id"],))
    if reminder_hours:
        for s in conn.execute("SELECT s.*, c.name FROM container_state s JOIN containers c ON c.id = s.container_id "
                              "WHERE s.alerted = 1 AND c.monitored = 1 AND s.last_alert_at < ?",
                              (now - reminder_hours * 3600,)).fetchall():
            if s["status"] in ("unhealthy", "critical") or (s["status"] == "degraded" and any_needs_look):
                reminders.append({"id": s["container_id"], "name": s["name"], "status": s["status"],
                                  "summary": s["summary"], "since": s["since"]})
                conn.execute("UPDATE container_state SET last_alert_at = ? WHERE container_id = ?", (now, s["container_id"]))
    messages = []
    serious = [p for p in problems if not p["needs_look_only"]]
    looks = [p for p in problems if p["needs_look_only"]]
    for group, kind in ((serious, "problems"), (looks, "needs_look")):
        if len(group) > COMBINE_OVER:
            body = "\n".join(f"• {p['name']}: {SHOWN[p['status']]}. {p['summary']}" for p in group)
            messages.append({"kind": kind, "title": f"{len(group)} containers need attention",
                             "body": body + _link(conn, None), "container_ids": [p["id"] for p in group]})
        else:
            for p in group:
                body = p["summary"] + (f"\nWhat to do: {p['what_to_do']}" if p["what_to_do"] else "")
                messages.append({"kind": kind, "title": f"{p['name']}: {SHOWN[p['status']]}",
                                 "body": body + _link(conn, p["id"]), "container_ids": [p["id"]]})
    if len(recoveries) > COMBINE_OVER:
        messages.append({"kind": "recoveries", "title": f"{len(recoveries)} containers recovered",
                         "body": ", ".join(r["name"] for r in recoveries) + _link(conn, None),
                         "container_ids": [r["id"] for r in recoveries]})
    else:
        for r in recoveries:
            how = "is working fine again" if r["status"] == "healthy" else "was stopped, so the problem is over"
            messages.append({"kind": "recoveries", "title": f"{r['name']}: recovered", "body": f"{r['name']} {how}."
                             + _link(conn, r["id"]), "container_ids": [r["id"]]})
    for r in reminders:
        hours = max(1, round((now - r["since"]) / 3600))
        messages.append({"kind": "reminders", "title": f"Still broken: {r['name']}",
                         "body": f"{SHOWN[r['status']]} for {hours} hour{'s' if hours != 1 else ''}. {r['summary']}"
                         + _link(conn, r["id"]), "container_ids": [r["id"]]})
    messages.extend(_checking_messages(conn, now))
    return messages


def _checking_messages(conn, now: float) -> list[dict]:
    """One message when containers couldn't be checked 3 rounds running, one when that ends."""
    stuck = conn.execute("SELECT s.container_id, s.summary, c.name, k.source FROM container_state s "
                         "JOIN containers c ON c.id = s.container_id LEFT JOIN checks k ON k.id = s.last_check_id "
                         "WHERE s.status = 'unknown' AND s.unknown_streak >= ? AND c.monitored = 1",
                         (UNKNOWN_ALERT_STREAK,)).fetchall()
    flag = conn.execute("SELECT value FROM settings WHERE key = 'notify._checking_alerted'").fetchone()
    alerted = bool(flag and json.loads(flag["value"]))
    msgs = []
    if stuck and not alerted:
        reason = stuck[0]["summary"].removeprefix("Couldn't check: ")
        names = ", ".join(s["name"] for s in stuck[:8]) + (" and more" if len(stuck) > 8 else "")
        title = "DockLlama can't reach its AI server" if stuck[0]["source"] == "ai" else "DockLlama can't check some containers"
        msgs.append({"kind": "checking", "title": title,
                     "body": f"Couldn't check {names} three times in a row. {reason}" + _link(conn, None), "container_ids": []})
        _set_flag(conn, True)
    elif not stuck and alerted:
        msgs.append({"kind": "checking", "title": "DockLlama is checking normally again",
                     "body": "Checks are working again." + _link(conn, None), "container_ids": []})
        _set_flag(conn, False)
    return msgs


def _set_flag(conn, value: bool) -> None:
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('notify._checking_alerted', ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (json.dumps(value), time.time()))


def queue_message(conn, kind: str, title: str, body: str, container_id: int | None = None) -> None:
    """For other parts of DockLlama (restarts, daily summary): send through the next pass."""
    conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'outgoing', ?)",
                 (time.time(), container_id, json.dumps({"kind": kind, "title": title, "body": body})))


async def deliver(messages: list[dict]) -> int:
    """Send each message to every target that wants its kind. Returns how many sends worked."""
    sent = 0
    for m in messages:
        def targets():
            with db.session() as conn:
                return _targets(conn, m["kind"])
        for t in await run_in_threadpool(targets):
            ok, problem = await asyncio.to_thread(send_now, t["url"], m["title"], m["body"])

            def record(t=t, ok=ok, problem=problem):
                with db.session() as conn:
                    conn.execute("UPDATE notify_targets SET last_ok = CASE WHEN ? THEN ? ELSE last_ok END, "
                                 "last_error = ? WHERE id = ?", (ok, time.time(), None if ok else problem, t["id"]))
                    conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'notify', ?)",
                                 (time.time(), (m["container_ids"] or [None])[0],
                                  json.dumps({"target": t["id"], "service": t["service"], "kind": m["kind"],
                                              "title": m["title"], "ok": ok, "problem": problem})))
            await run_in_threadpool(record)
            sent += ok
    return sent


async def process() -> int:
    """One pass: plan from the database (in one transaction), then send. Nothing goes out during
    quiet hours; what happened is sent when they end (if it's still true then)."""
    def plan():
        from .restarts import in_quiet_hours
        with db.session() as conn, db.transaction(conn):
            if in_quiet_hours(conn):
                return []
            msgs = plan_messages(conn)
            for e in conn.execute("SELECT * FROM events WHERE kind = 'outgoing' AND handled = 0 ORDER BY id").fetchall():
                d = json.loads(e["data"])
                msgs.append({"kind": d["kind"], "title": d["title"], "body": d["body"] + _link(conn, e["container_id"]),
                             "container_ids": [e["container_id"]] if e["container_id"] else []})
                conn.execute("UPDATE events SET handled = 1 WHERE id = ?", (e["id"],))
            return msgs
    messages = await run_in_threadpool(plan)
    return await deliver(messages) if messages else 0


class Notifier:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="notifier")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                await process()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never stop notifying because one pass failed
                log.exception("Notification pass failed")
                await asyncio.sleep(10)


NOTIFIER = Notifier()
