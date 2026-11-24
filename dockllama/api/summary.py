"""Settings → Notifications → Daily summary."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, settings, summary

router = APIRouter(prefix="/api/summary", tags=["summary"])


@router.get("")
def get_summary():
    with db.session() as conn:
        return {"enabled": settings.get(conn, "summary.enabled"), "time": settings.get(conn, "summary.time"),
                "last_sent": settings.get(conn, "summary.last_sent"), "timezone": settings.get(conn, "general.timezone")}


class SummaryBody(BaseModel):
    enabled: bool | None = None
    time: str | None = None


@router.put("")
def put_summary(body: SummaryBody):
    with db.session() as conn:
        try:
            if body.enabled is not None:
                settings.set(conn, "summary.enabled", body.enabled)
            if body.time is not None:
                settings.set(conn, "summary.time", body.time)
        except settings.SettingError as exc:
            raise HTTPException(400, f"The time {exc}.") from exc
    return get_summary()


@router.post("/preview")
async def preview():
    title, body, by_ai = await summary.build()
    return {"title": title, "body": body, "by_ai": by_ai}


@router.post("/send-now")
async def send_now():
    return await summary.send()
