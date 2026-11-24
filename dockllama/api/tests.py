"""Settings → AI: testing models on this server, and the "better model found" banner."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import ai_queue, ai_servers, catalog, db, model_tests
from ..model_tests import RUNNER
from ..scenarios import SCENARIOS

router = APIRouter(prefix="/api/ai", tags=["ai-tests"])


def _server(conn):
    row = ai_servers.connected(conn)
    if row is None:
        raise HTTPException(409, "No AI server is connected yet.")
    return row


def _busy_message(conn, run) -> str:
    p = RUNNER.progress
    if run["status"] == "paused":
        return "A model test is paused. Resume or cancel it first."
    if run["status"] == "waiting":
        return "Model tests are queued and start in a moment."
    if p is not None and RUNNER.current_run == run["id"]:
        return (f"Testing {p.name} (question {min(p.scenario + 1, len(SCENARIOS))} of {len(SCENARIOS)}). "
                "Wait for it to finish or cancel it.")
    return "A model test is running. Wait for it to finish or cancel it."


def _run_view(conn, run, hardware: str) -> dict:
    tests = conn.execute("SELECT * FROM model_tests WHERE run_id = ? ORDER BY position", (run["id"],)).fetchall()
    models = {m.name: m for m in ai_servers.installed_models(conn, run["server_id"])}
    latest = model_tests.latest_results(conn, run["server_id"])
    p = RUNNER.progress if RUNNER.current_run == run["id"] else None
    remaining = 0.0
    items = []
    for t in tests:
        item = {"name": t["name"], "status": t["status"], "score": t["score"], "sec_per_check": t["sec_per_check"],
                "fit": t["fit"], "error": t["error"], "cut_off": bool(t["cut_off"])}
        if p is not None and p.test_id == t["id"]:
            item.update(scenario=p.scenario, of=len(SCENARIOS), phase=p.phase)
        if t["status"] in ("queued", "running") and t["name"] in models:
            est = model_tests.estimate_seconds(models[t["name"]], hardware, latest.get(t["name"]))
            if p is not None and p.test_id == t["id"]:
                est *= max(0.1, 1 - p.scenario / len(SCENARIOS))
            remaining += est
        items.append(item)
    finished = sum(1 for t in tests if t["status"] not in ("queued", "running"))
    best = model_tests.best_result({i["name"]: i for i in items if i["name"] in models})
    return {"run_id": run["id"], "kind": run["kind"], "mode": run["mode"], "status": run["status"],
            "note": run["note"], "start_after": run["start_after"], "models": items, "finished": finished,
            "total": len(tests), "remaining_s": round(remaining), "suggested": best["name"] if best else None}


@router.get("/tests")
def tests_status():
    with db.session() as conn:
        server = ai_servers.connected(conn)
        if server is None:
            return {"busy": False, "active": None, "scheduled": None, "results": {}}
        busy = model_tests.busy_run(conn)
        active = conn.execute("SELECT * FROM test_runs WHERE server_id = ? AND status IN "
                              "('running', 'paused', 'waiting') ORDER BY status = 'waiting', id LIMIT 1",
                              (server["id"],)).fetchone()
        scheduled = conn.execute("SELECT * FROM test_runs WHERE server_id = ? AND status = 'scheduled' ORDER BY id LIMIT 1",
                                 (server["id"],)).fetchone()
        last_watch = conn.execute("SELECT * FROM test_runs WHERE server_id = ? AND mode = 'watch' ORDER BY id DESC LIMIT 1",
                                  (server["id"],)).fetchone()
        return {
            "busy": busy is not None,
            "busy_message": _busy_message(conn, busy) if busy is not None else None,
            "active": _run_view(conn, active, server["hardware"]) if active else None,
            "scheduled": _run_view(conn, scheduled, server["hardware"]) if scheduled else None,
            "watched": _run_view(conn, last_watch, server["hardware"]) if last_watch else None,
            "results": model_tests.latest_results(conn, server["id"]),
            "checks_paused": ai_queue.checks_paused,
        }


@router.get("/tests/plan")
def test_all_plan():
    """What "Test all" would do: every usable model, ticked unless it won't fit, with a time estimate."""
    with db.session() as conn:
        server = _server(conn)
        models = [m for m in ai_servers.installed_models(conn, server["id"]) if m.usable]
        latest = model_tests.latest_results(conn, server["id"])
        tonight_at = model_tests.tonight(conn)
    suggestion = catalog.best_guess(models, server["hardware"])
    first = server["model"] or (suggestion.name if suggestion else None)
    models.sort(key=lambda m: (m.name != first, m.size))  # best guess first, then small to large
    items = []
    for m in models:
        fits = catalog.fits(m.size / catalog.GIB, server["hardware"])
        items.append({"name": m.name, "size_gb": round(m.size / 1e9, 1), "fits": fits, "ticked": fits,
                      "estimate_s": round(model_tests.estimate_seconds(m, server["hardware"], latest.get(m.name))),
                      "last": latest.get(m.name)})
    return {"models": items, "tonight_at": tonight_at,
            "total_s": sum(i["estimate_s"] for i in items if i["ticked"])}


class StartBody(BaseModel):
    names: list[str]
    kind: str = "one"  # one | all
    mode: str = "background"  # background | watch
    tonight: bool = False
    queue: bool = False  # if a test is running, wait for it instead of refusing (Quick start)


@router.post("/tests")
async def start_tests(body: StartBody):
    if body.kind not in ("one", "all") or body.mode not in ("background", "watch"):
        raise HTTPException(400, "Unknown kind of test.")
    names = list(dict.fromkeys(n.strip() for n in body.names if n.strip()))
    if not names:
        raise HTTPException(400, "Pick at least one model to test.")
    if body.kind == "one" and len(names) != 1:
        raise HTTPException(400, "A single test is for one model.")

    def create():
        with db.session() as conn, db.transaction(conn):
            server = _server(conn)
            busy = model_tests.busy_run(conn)
            if busy is not None and not body.tonight and not body.queue:
                raise HTTPException(409, _busy_message(conn, busy))
            if busy is not None and body.queue:
                # Don't test again what's already being tested or waiting to be.
                pending = {r["name"] for r in conn.execute(
                    "SELECT t.name FROM model_tests t JOIN test_runs r ON r.id = t.run_id WHERE t.status IN "
                    "('queued', 'running') AND r.status IN ('waiting', 'running', 'paused')")}
                names[:] = [n for n in names if n not in pending]
                if not names:
                    return None
            if body.tonight and conn.execute("SELECT 1 FROM test_runs WHERE status = 'scheduled'").fetchone():
                raise HTTPException(409, "A test is already set to run tonight. Cancel it first.")
            for name in names:
                row = conn.execute("SELECT * FROM models WHERE server_id = ? AND name = ? AND installed = 1",
                                   (server["id"], name)).fetchone()
                if row is None:
                    raise HTTPException(404, f"{name} isn't installed on this server.")
                model = ai_servers.model_from_row(row)
                if not model.usable:
                    raise HTTPException(400, f"{name} can't be used: {model.unusable_reason}.")
            start_after = model_tests.tonight(conn) if body.tonight else None
            if body.kind == "all" and not body.tonight and busy is None:
                # A waiting automatic test of new models is folded into Test all.
                for w in conn.execute("SELECT id FROM test_runs WHERE status = 'waiting'").fetchall():
                    conn.execute("UPDATE model_tests SET status = 'cancelled' WHERE run_id = ? AND status = 'queued'",
                                 (w["id"],))
                    conn.execute("UPDATE test_runs SET status = 'cancelled', finished_at = ?, note = ? WHERE id = ?",
                                 (time.time(), "Included in Test all.", w["id"]))
            mode = "background" if body.tonight else body.mode
            return model_tests.create_run(conn, server["id"], names, body.kind, mode, start_after,
                                          queued=busy is not None and body.queue)

    run_id = await run_in_threadpool(create)
    RUNNER.wake()
    return {"ok": True, "run_id": run_id}


def _get_run(conn, run_id: int):
    run = conn.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise HTTPException(404, "That test run isn't known.")
    return run


@router.post("/tests/{run_id}/pause")
def pause(run_id: int):
    with db.session() as conn, db.transaction(conn):
        run = _get_run(conn, run_id)
        if run["status"] != "running":
            raise HTTPException(409, "That test isn't running.")
        conn.execute("UPDATE test_runs SET status = 'paused', note = 'Paused by you.' WHERE id = ?", (run_id,))
    RUNNER.interrupt(run_id, "pause")
    return {"ok": True}


@router.post("/tests/{run_id}/resume")
def resume(run_id: int):
    with db.session() as conn, db.transaction(conn):
        run = _get_run(conn, run_id)
        if run["status"] != "paused":
            raise HTTPException(409, "That test isn't paused.")
        other = conn.execute("SELECT 1 FROM test_runs WHERE status = 'running' AND id != ?", (run_id,)).fetchone()
        if other:
            raise HTTPException(409, "Another test is running. Wait for it to finish.")
        conn.execute("UPDATE test_runs SET status = 'running', note = NULL WHERE id = ?", (run_id,))
    RUNNER.wake()
    return {"ok": True}


@router.post("/tests/{run_id}/cancel")
def cancel(run_id: int):
    with db.session() as conn, db.transaction(conn):
        run = _get_run(conn, run_id)
        if run["status"] not in ("running", "paused", "waiting", "scheduled"):
            raise HTTPException(409, "That test has already finished.")
        conn.execute("UPDATE model_tests SET status = 'cancelled' WHERE run_id = ? AND status = 'queued'", (run_id,))
        conn.execute("UPDATE test_runs SET status = 'cancelled', finished_at = ?, note = 'Cancelled by you.' WHERE id = ?",
                     (time.time(), run_id))
    RUNNER.interrupt(run_id, "cancel")
    RUNNER.wake()
    return {"ok": True}


@router.post("/tests/{run_id}/background")
def to_background(run_id: int):
    """Leaving the wait-and-watch screen turns the run into a background run."""
    with db.session() as conn:
        _get_run(conn, run_id)
        conn.execute("UPDATE test_runs SET mode = 'background' WHERE id = ? AND mode = 'watch'", (run_id,))
    ai_queue.checks_paused = False
    return {"ok": True}


@router.get("/tests/history")
def test_history(name: str):
    with db.session() as conn:
        server = _server(conn)
        return {"name": name, "results": model_tests.history(conn, server["id"], name)}


@router.get("/banner")
def get_banner():
    with db.session() as conn, db.transaction(conn):
        return {"banner": model_tests.banner(conn)}


class DismissBody(BaseModel):
    run_id: int


@router.post("/banner/dismiss")
def dismiss_banner(body: DismissBody):
    with db.session() as conn:
        conn.execute("UPDATE test_runs SET banner = 'dismissed' WHERE id = ? AND banner = 'pending'", (body.run_id,))
    return {"ok": True}
