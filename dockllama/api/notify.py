"""Settings → Notifications (and setup step 5)."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import db, notify, settings

router = APIRouter(prefix="/api/notify", tags=["notify"])

TEST_TITLE = "DockLlama test message"
TEST_BODY = "If you can read this, DockLlama can reach you here. You'll get a message when a container has a problem."


def _describe(service: str, fields: dict) -> str:
    """A safe one-line description (no secrets)."""
    if service == "ntfy":
        return f"ntfy topic {fields.get('topic')} on {fields.get('server') or 'ntfy.sh'}"
    if service == "discord":
        return "Discord webhook …" + str(fields.get("webhook", ""))[-6:]
    if service == "telegram":
        return f"Telegram chat {fields.get('chat_id')}"
    if service == "gotify":
        return f"Gotify at {fields.get('server')}"
    if service == "pushover":
        return "Pushover user …" + str(fields.get("user_key", ""))[-4:]
    if service == "email":
        return f"Email to {fields.get('address')}"
    return "Apprise URL"


def _view(r) -> dict:
    fields = json.loads(r["fields"])
    return {"id": r["id"], "service": r["service"], "description": _describe(r["service"], fields),
            "events": {**notify.DEFAULT_EVENTS, **json.loads(r["events"])}, "enabled": bool(r["enabled"]),
            "last_ok": r["last_ok"], "last_error": r["last_error"],
            "subscribe": notify.subscribe_link(r["service"], fields)}


class TargetBody(BaseModel):
    service: str
    fields: dict
    events: dict | None = None
    base_url: str | None = None  # the browser's own address, for links in messages


def _build(body: TargetBody) -> str:
    if body.service not in notify.SERVICES:
        raise HTTPException(400, "Pick one of the listed services.")
    try:
        url = notify.build_url(body.service, body.fields)
    except notify.NotifyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not notify._valid_url(url):
        raise HTTPException(400, "DockLlama couldn't use those details. Check them and try again.")
    return url


@router.get("/targets")
def targets():
    with db.session() as conn:
        return {"targets": [_view(r) for r in conn.execute("SELECT * FROM notify_targets ORDER BY id")],
                "reminder_hours": settings.get(conn, "notify.reminder_hours"),
                "base_url": settings.get(conn, "general.base_url")}


@router.post("/targets")
def add_target(body: TargetBody):
    url = _build(body)
    events = {k: bool(v) for k, v in (body.events or {}).items() if k in notify.EVENT_KINDS}
    clean_fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.fields.items()}
    with db.session() as conn:
        tid = conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) VALUES (?, ?, ?, ?, ?)",
                           (body.service, json.dumps(clean_fields), url, json.dumps(events), time.time())).lastrowid
        if body.base_url and body.base_url.startswith(("http://", "https://")):
            settings.set(conn, "general.base_url", body.base_url.rstrip("/"))
        row = conn.execute("SELECT * FROM notify_targets WHERE id = ?", (tid,)).fetchone()
    return _view(row)


class UpdateBody(BaseModel):
    events: dict | None = None
    enabled: bool | None = None


@router.patch("/targets/{tid}")
def update_target(tid: int, body: UpdateBody):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM notify_targets WHERE id = ?", (tid,)).fetchone()
        if row is None:
            raise HTTPException(404, "That notification target isn't known.")
        if body.events is not None:
            events = {**json.loads(row["events"]), **{k: bool(v) for k, v in body.events.items() if k in notify.EVENT_KINDS}}
            conn.execute("UPDATE notify_targets SET events = ? WHERE id = ?", (json.dumps(events), tid))
        if body.enabled is not None:
            conn.execute("UPDATE notify_targets SET enabled = ? WHERE id = ?", (int(body.enabled), tid))
        return _view(conn.execute("SELECT * FROM notify_targets WHERE id = ?", (tid,)).fetchone())


@router.delete("/targets/{tid}")
def delete_target(tid: int):
    with db.session() as conn:
        conn.execute("DELETE FROM notify_targets WHERE id = ?", (tid,))
    return {"ok": True}


@router.post("/test")
async def test_unsaved(body: TargetBody):
    """Send a test with details that aren't saved yet (the Send test button in the form)."""
    url = _build(body)
    ok, problem = await asyncio.to_thread(notify.send_now, url, TEST_TITLE, TEST_BODY)
    return {"ok": ok, "problem": problem}


@router.post("/targets/{tid}/test")
async def test_saved(tid: int):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM notify_targets WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, "That notification target isn't known.")
    ok, problem = await asyncio.to_thread(notify.send_now, row["url"], TEST_TITLE, TEST_BODY)
    with db.session() as conn:
        conn.execute("UPDATE notify_targets SET last_ok = CASE WHEN ? THEN ? ELSE last_ok END, last_error = ? WHERE id = ?",
                     (ok, time.time(), None if ok else problem, tid))
    return {"ok": ok, "problem": problem}


@router.get("/ntfy-topic")
def ntfy_topic():
    return {"topic": notify.new_ntfy_topic()}


@router.get("/qr")
def qr(text: str):
    if not text.startswith(("http://", "https://")) or len(text) > 300:
        raise HTTPException(400, "Only links can be turned into a QR code.")
    return Response(notify.qr_svg(text), media_type="image/svg+xml")


class SettingsBody(BaseModel):
    reminder_hours: int


@router.put("/settings")
def put_settings(body: SettingsBody):
    with db.session() as conn:
        try:
            settings.set(conn, "notify.reminder_hours", body.reminder_hours)
        except settings.SettingError as exc:
            raise HTTPException(400, f"Reminder hours {exc}.") from exc
    return {"ok": True}


@router.get("/log")
def recent():
    with db.session() as conn:
        rows = conn.execute("SELECT at, data FROM events WHERE kind = 'notify' ORDER BY id DESC LIMIT 30").fetchall()
    return {"sent": [{"at": r["at"], **json.loads(r["data"])} for r in rows]}
