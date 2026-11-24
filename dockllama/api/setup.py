"""The setup guide (/welcome): where the user is, and finishing it."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from .. import ai_servers, db, model_tests, settings
from ..checks import SCHEDULER

router = APIRouter(prefix="/api/setup", tags=["setup"])

SECONDS_PER_CHECK_WITHOUT_AI = 0.2  # Docker facts and rules only
SECONDS_PER_CHECK_GUESS = 5.0  # an untested model; replaced by the test result once there is one


@router.get("/state")
def state():
    with db.session() as conn:
        server = ai_servers.connected(conn)
        per_check, source = SECONDS_PER_CHECK_WITHOUT_AI, "no_ai"
        if server is not None and server["model"]:
            result = model_tests.latest_results(conn, server["id"]).get(server["model"])
            if result and result.get("sec_per_check"):
                per_check, source = result["sec_per_check"], "tested"
            else:
                per_check, source = SECONDS_PER_CHECK_GUESS, "guess"
        watched = conn.execute("SELECT COUNT(*) n FROM containers WHERE monitored = 1 AND gone = 0").fetchone()["n"]
        return {
            "complete": settings.get(conn, "setup.complete"),
            "ai_connected": server is not None,
            "model": server["model"] if server else None,
            "interval_minutes": settings.get(conn, "checks.interval_minutes"),
            "seconds_per_check": per_check, "per_check_source": source,
            "watched": watched,
        }


class FinishBody(BaseModel):
    complete: bool = True


@router.post("/finish")
def finish(body: FinishBody):
    with db.session() as conn:
        settings.set(conn, "setup.complete", body.complete)
    if body.complete:
        SCHEDULER.run_now()  # the dashboard shows the first round straight away
    return {"ok": True}
