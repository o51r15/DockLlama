"""Settings → Account → Bring over a DockLlama v1 setup."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, importer

router = APIRouter(prefix="/api/import", tags=["import"])


class ImportBody(BaseModel):
    config_text: str = Field(max_length=2_000_000)
    apply: bool = False


@router.post("/v1")
def import_v1(body: ImportBody):
    try:
        p = importer.plan(body.config_text)
    except importer.ImportError_ as exc:
        raise HTTPException(400, str(exc)) from exc
    if not body.apply:
        preview = asdict(p)
        preview["alert_urls"] = [u.split("://", 1)[0] + "://…" for u in p.alert_urls]  # don't echo tokens back
        return {"preview": preview}
    with db.session() as conn, db.transaction(conn):
        return {"result": importer.apply(conn, p)}
