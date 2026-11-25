"""Settings → Advanced: data retention, log lines per check, and forgetting old AI servers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import db, settings

router = APIRouter(prefix="/api/advanced", tags=["advanced"])

KEYS = {"retention_days": "data.retention_days", "log_lines": "checks.log_lines"}
LABELS = {"retention_days": "How long to keep history", "log_lines": "Log lines per check"}


@router.get("")
def get_advanced():
    with db.session() as conn:
        out = {k: settings.get(conn, v) for k, v in KEYS.items()}
        oldest = conn.execute("SELECT MIN(at) t FROM checks").fetchone()["t"]
        out["oldest_check"] = oldest
        return out


class AdvancedBody(BaseModel):
    retention_days: int | None = None
    log_lines: int | None = None


@router.put("")
async def put_advanced(body: AdvancedBody):
    changes = body.model_dump(exclude_none=True)

    def save():
        with db.session() as conn, db.transaction(conn):
            for key, value in changes.items():
                try:
                    settings.set(conn, KEYS[key], value)
                except settings.SettingError as exc:
                    raise HTTPException(400, f"{LABELS[key]} {exc}.") from exc
            if "retention_days" in changes:  # a shorter period takes effect now, not tomorrow
                from ..checks import prune
                prune(conn)
    await run_in_threadpool(save)
    return get_advanced()


@router.get("/servers")
def old_servers():
    """AI servers DockLlama has used before, other than the connected one, with what's stored."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT s.id, s.name, s.address, s.last_seen, "
            "(SELECT COUNT(*) FROM models m WHERE m.server_id = s.id) models, "
            "(SELECT COUNT(*) FROM model_tests t WHERE t.server_id = s.id AND t.status = 'done') tests "
            "FROM ai_servers s WHERE s.connected = 0 ORDER BY s.last_seen DESC").fetchall()
        return {"servers": [dict(r) for r in rows]}


@router.delete("/servers/{server_id}")
def forget_server(server_id: int):
    with db.session() as conn, db.transaction(conn):
        row = conn.execute("SELECT connected FROM ai_servers WHERE id = ?", (server_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "That AI server isn't stored any more.")
        if row["connected"]:
            raise HTTPException(409, "That's the AI server DockLlama is using now. Connect a different one first.")
        conn.execute("DELETE FROM ai_servers WHERE id = ?", (server_id,))  # its models, settings and tests go with it
    return {"ok": True}
