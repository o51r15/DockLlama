"""Settings → Advanced → AI instructions: the built-in Default plus alternative prompts."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import ai_servers, db, model_tests, prompts, settings
from ..evaluate import ANSWER_FORMAT, DEFAULT_INSTRUCTIONS
from ..model_tests import RUNNER

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _view(row) -> dict:
    return {"id": row["id"], "number": row["number"], "name": row["name"], "label": prompts.label(row),
            "text": row["text"], "revision": row["revision"], "updated_at": row["updated_at"]}


@router.get("")
def list_prompts():
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM prompts ORDER BY number").fetchall()
        return {"default": DEFAULT_INSTRUCTIONS, "answer_format": ANSWER_FORMAT,
                "active": settings.get(conn, "ai.active_prompt"), "prompts": [_view(r) for r in rows]}


class PromptBody(BaseModel):
    name: str = ""
    text: str | None = None  # new prompts start as a copy of the Default


def _guard(fn):
    try:
        return fn()
    except prompts.PromptError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("")
def create_prompt(body: PromptBody):
    with db.session() as conn, db.transaction(conn):
        pid = _guard(lambda: prompts.create(conn, body.text if body.text is not None else DEFAULT_INSTRUCTIONS, body.name))
        return _view(prompts.get(conn, pid))


class UseBody(BaseModel):
    id: int  # 0 = the Default


@router.put("/active")
def use_prompt(body: UseBody):
    with db.session() as conn, db.transaction(conn):
        _guard(lambda: prompts.use(conn, body.id))
    return list_prompts()


@router.put("/{prompt_id}")
def update_prompt(prompt_id: int, body: PromptBody):
    with db.session() as conn, db.transaction(conn):
        _guard(lambda: prompts.update(conn, prompt_id, body.text or "", body.name))
        return _view(prompts.get(conn, prompt_id))


@router.delete("/{prompt_id}")
def delete_prompt(prompt_id: int):
    with db.session() as conn, db.transaction(conn):
        if prompts.get(conn, prompt_id) is None:
            raise HTTPException(404, "That prompt doesn't exist any more.")
        busy = model_tests.busy_run(conn)
        if busy is not None and busy["prompt_id"] == prompt_id:
            raise HTTPException(409, "This prompt is being tested. Cancel the test first.")
        prompts.delete(conn, prompt_id)
    return list_prompts()


class TestBody(BaseModel):
    names: list[str]


@router.post("/{prompt_id}/test")
async def test_prompt(prompt_id: int, body: TestBody):
    """Test this prompt on the chosen models. Uses the normal test queue (one test at a time);
    the results are shown only on the prompt screen."""
    names = list(dict.fromkeys(n.strip() for n in body.names if n.strip()))
    if not names:
        raise HTTPException(400, "Pick at least one model to test.")

    def create():
        with db.session() as conn, db.transaction(conn):
            if prompts.get(conn, prompt_id) is None:
                raise HTTPException(404, "That prompt doesn't exist any more.")
            server = ai_servers.connected(conn)
            if server is None:
                raise HTTPException(400, "Connect an AI server first.")
            busy = model_tests.busy_run(conn)
            if busy is not None:
                raise HTTPException(409, "A model test is already running. Wait for it to finish or cancel it "
                                         "in Settings → AI.")
            for name in names:
                row = conn.execute("SELECT * FROM models WHERE server_id = ? AND name = ? AND installed = 1",
                                   (server["id"], name)).fetchone()
                if row is None or not ai_servers.model_from_row(row).usable:
                    raise HTTPException(400, f"{name} isn't installed or can't be used.")
            return model_tests.create_run(conn, server["id"], names, "prompt", "background", prompt_id=prompt_id)

    run_id = await run_in_threadpool(create)
    RUNNER.wake()
    return {"ok": True, "run_id": run_id}


@router.get("/{prompt_id}/results")
def prompt_results(prompt_id: int):
    """Each installed model: its latest result with this prompt next to its latest with the Default."""
    with db.session() as conn:
        row = prompts.get(conn, prompt_id)
        if row is None:
            raise HTTPException(404, "That prompt doesn't exist any more.")
        server = ai_servers.connected(conn)
        if server is None:
            return {"models": [], "busy": None}
        default = model_tests.latest_results(conn, server["id"])
        mine = {}
        for t in conn.execute(
                "SELECT * FROM model_tests WHERE server_id = ? AND prompt_id = ? AND status IN "
                "('done', 'too_slow', 'failed') ORDER BY finished_at", (server["id"], prompt_id)):
            d = model_tests.result_dict(t, None)
            d["outdated"] = t["prompt_rev"] is not None and t["prompt_rev"] != row["revision"]
            mine[t["name"]] = d
        queued = {t["name"]: t["status"] for t in conn.execute(
            "SELECT t.name, t.status FROM model_tests t JOIN test_runs r ON r.id = t.run_id WHERE t.prompt_id = ? "
            "AND t.status IN ('queued', 'running') AND r.status IN ('waiting', 'running', 'paused')", (prompt_id,))}
        out = []
        for m in ai_servers.installed_models(conn, server["id"]):
            if not m.usable:
                continue
            out.append({"name": m.name, "qwen_based": m.qwen_based, "default": default.get(m.name),
                        "this": mine.get(m.name), "pending": queued.get(m.name)})
        busy = model_tests.busy_run(conn)
        return {"models": out, "busy": dict(busy) if busy is not None else None, "in_use": server["model"]}
