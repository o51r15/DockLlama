"""Settings → AI: connecting to Ollama, models, suggestions, downloads, Advanced settings."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import advanced, ai_servers, catalog, db, model_tests
from ..ollama import OllamaClient, OllamaError, discover, inventory_fingerprint, normalize_url

router = APIRouter(prefix="/api/ai", tags=["ai"])


def _problem(exc: OllamaError) -> dict:
    return {"kind": exc.kind, "message": str(exc)}


def _connected_or_409():
    with db.session() as conn:
        row = ai_servers.connected(conn)
    if row is None:
        raise HTTPException(409, "No AI server is connected yet.")
    return row


class AddressBody(BaseModel):
    address: str


class ConnectBody(BaseModel):
    address: str
    decision: str | None = None  # None (ask me) | "same" | "new"
    server_id: int | None = None  # with decision="same"


@router.get("/discover")
async def discover_servers():
    return {"found": await discover()}


@router.post("/check")
async def check(body: AddressBody):
    """Try an address without saving anything."""
    try:
        url = normalize_url(body.address)
        client = OllamaClient(url)
        version = await client.check()
        tags = await client.tags()
    except OllamaError as exc:
        return {"ok": False, "problem": _problem(exc)}
    usable = sum(1 for t in tags if not t.get("remote_host"))
    return {"ok": True, "address": url, "version": version, "model_count": usable}


@router.post("/connect")
async def connect(body: ConnectBody):
    try:
        url = normalize_url(body.address)
        client = OllamaClient(url)
        version = await client.check()
        tags = await client.tags()
        models = await client.models()
    except OllamaError as exc:
        return {"ok": False, "problem": _problem(exc)}
    fingerprint = inventory_fingerprint(tags)

    def work():
        with db.session() as conn, db.transaction(conn):
            current = ai_servers.connected(conn)
            match = ai_servers.find_match(conn, url, fingerprint)
            if body.decision is None and match is not None:
                if current is not None and current["id"] == match.server_id:
                    reuse = match.server_id  # reconnecting to the same server: nothing to ask
                else:
                    return {"ok": False, "needs_confirm": {
                        "server_id": match.server_id, "name": match.name,
                        "last_address": match.last_address, "how": match.how,
                        "tested_models": match.tested_models,
                    }}
            elif body.decision == "same":
                if body.server_id is None:
                    raise HTTPException(400, "Which server is it the same as?")
                if conn.execute("SELECT 1 FROM ai_servers WHERE id = ?", (body.server_id,)).fetchone() is None:
                    raise HTTPException(404, "That server isn't known.")
                reuse = body.server_id
            else:
                reuse = None
            server_id = ai_servers.connect(conn, url, version, fingerprint, reuse)
            ai_servers.save_models(conn, server_id, models, fingerprint)
            return {"ok": True, "server_id": server_id, "restored": reuse is not None}

    return await run_in_threadpool(work)


def _status_row():
    with db.session() as conn:
        row = ai_servers.connected(conn)
        others = conn.execute("SELECT COUNT(*) n FROM ai_servers WHERE connected = 0").fetchone()["n"]
    return row, others


@router.get("/status")
async def status():
    row, others = await run_in_threadpool(_status_row)
    if row is None:
        return {"connected": False, "other_servers": others}
    reachable, problem = True, None
    try:
        await OllamaClient(row["address"]).check(timeout=4)
    except OllamaError as exc:
        reachable, problem = False, _problem(exc)
    return {
        "connected": True, "server_id": row["id"], "name": row["name"], "address": row["address"],
        "version": row["version"], "model": row["model"], "hardware": row["hardware"],
        "hardware_choices": {k: v["label"] for k, v in catalog.HARDWARE.items()},
        "reachable": reachable, "problem": problem, "other_servers": others,
    }


class HardwareBody(BaseModel):
    hardware: str


@router.put("/hardware")
def set_hardware(body: HardwareBody):
    if body.hardware not in catalog.HARDWARE:
        raise HTTPException(400, "Pick one of the listed options.")
    row = _connected_or_409()
    with db.session() as conn:
        conn.execute("UPDATE ai_servers SET hardware = ? WHERE id = ?", (body.hardware, row["id"]))
    return {"ok": True}


class NameBody(BaseModel):
    name: str


@router.put("/server-name")
def rename_server(body: NameBody):
    name = body.name.strip()
    if not 1 <= len(name) <= 60:
        raise HTTPException(400, "Give the server a name of 1–60 characters.")
    row = _connected_or_409()
    with db.session() as conn:
        conn.execute("UPDATE ai_servers SET name = ? WHERE id = ?", (name, row["id"]))
    return {"ok": True}


@router.get("/models")
async def list_models():
    """Installed models on the connected server, refreshed from Ollama, with the suggestion."""
    row = await run_in_threadpool(_connected_or_409)
    client = OllamaClient(row["address"])
    problem = None
    loaded: set[str] = set()
    try:
        tags = await client.tags()
        models = await client.models()
        loaded = {m.get("name") for m in await client.ps()}
        fingerprint = inventory_fingerprint(tags)

        def save():
            with db.session() as conn, db.transaction(conn):
                new = ai_servers.save_models(conn, row["id"], models, fingerprint)
                return model_tests.queue_new_models(conn, row["id"], new)
        if await run_in_threadpool(save):
            model_tests.RUNNER.wake()
    except OllamaError as exc:
        problem = _problem(exc)  # fall back to what we last saw

    def read():
        with db.session() as conn:
            fresh = conn.execute("SELECT * FROM ai_servers WHERE id = ?", (row["id"],)).fetchone()
            settings_rows = {
                r["name"] for r in conn.execute("SELECT name FROM model_settings WHERE server_id = ?", (row["id"],))
            }
            results = model_tests.latest_results(conn, row["id"])
            return fresh, ai_servers.installed_models(conn, row["id"]), settings_rows, results
    fresh, models, customised, results = await run_in_threadpool(read)
    suggestion = _suggest(models, results, fresh["hardware"], loaded)
    items = []
    for m in sorted(models, key=lambda m: (not m.usable, m.name)):
        entry = catalog.catalog_entry(m.name)
        d = m.to_dict()
        d.update({
            "size_gb": round(m.size / 1e9, 1),
            "fits": catalog.fits(m.size / catalog.GIB, fresh["hardware"]),
            "catalog_score": entry["score"] if entry else None,
            "loaded": m.name in loaded,
            "in_use": m.name == fresh["model"],
            "suggested": suggestion is not None and m.name == suggestion.name,
            "customised": m.name in customised,
            "result": results.get(m.name),
        })
        items.append(d)
    return {
        "models": items,
        "model": fresh["model"],
        "suggestion": {"name": suggestion.name, "reason": suggestion.reason} if suggestion else None,
        "downloads": catalog.download_suggestions(models, fresh["hardware"]),
        "problem": problem,
    }


def _suggest(models, results: dict, hardware: str, loaded: set[str]):
    """Tested results on this server beat the catalog's guess (plan 8A)."""
    usable = {m.name for m in models if m.usable}
    best = model_tests.best_result({n: r for n, r in results.items() if n in usable})
    if best is not None:
        return catalog.Suggestion(best["name"], f"Best result on this server: {model_tests.describe(best)}.")
    return catalog.best_guess(models, hardware, loaded)


@router.put("/model")
def choose_model(body: NameBody):
    row = _connected_or_409()
    with db.session() as conn:
        m = conn.execute(
            "SELECT * FROM models WHERE server_id = ? AND name = ? AND installed = 1", (row["id"], body.name)
        ).fetchone()
        if m is None:
            raise HTTPException(404, "That model isn't installed on this server.")
        model = ai_servers.model_from_row(m)
        if not model.usable:
            raise HTTPException(400, f"{model.name} can't be used: {model.unusable_reason}.")
        conn.execute("UPDATE ai_servers SET model = ? WHERE id = ?", (body.name, row["id"]))
    return {"ok": True, "model": body.name}


# --- downloads -----------------------------------------------------------------

PULLS: dict[str, dict] = {}  # model name -> progress (one server at a time, so the name is enough)
_pull_tasks: set[asyncio.Task] = set()


async def _run_pull(address: str, name: str) -> None:
    state = PULLS[name]
    try:
        async for event in OllamaClient(address).pull(name):
            state["status"] = event.get("status", state["status"])
            if event.get("total"):
                state["total"], state["completed"] = event["total"], event.get("completed", 0)
        state.update(done=True, status="Downloaded")
        await _after_download(address, name)
    except OllamaError as exc:
        state.update(done=True, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - never leave a pull stuck at "downloading"
        state.update(done=True, error=f"The download failed: {exc}")
    state["finished_at"] = time.time()


async def _after_download(address: str, name: str) -> None:
    """Save the new model and test it automatically (plan 8A: "tested automatically")."""
    client = OllamaClient(address)
    try:
        tags = await client.tags()
        models = await client.models()
    except OllamaError:
        return  # the next visit to Settings → AI picks it up

    def save():
        with db.session() as conn, db.transaction(conn):
            server = ai_servers.connected(conn)
            if server is None or server["address"] != address:
                return None
            new = ai_servers.save_models(conn, server["id"], models, inventory_fingerprint(tags))
            return model_tests.queue_new_models(conn, server["id"], sorted(set(new) | {name}))
    if await run_in_threadpool(save):
        model_tests.RUNNER.wake()


@router.post("/pull")
async def start_pull(body: NameBody):
    row = await run_in_threadpool(_connected_or_409)
    name = body.name.strip()
    if not name or len(name) > 200:
        raise HTTPException(400, "That isn't a model name.")
    running = [n for n, s in PULLS.items() if not s.get("done")]
    if running:
        raise HTTPException(409, f"{running[0]} is still downloading. Wait for it to finish.")
    PULLS[name] = {"name": name, "status": "Starting download", "total": None, "completed": 0,
                   "done": False, "error": None, "started_at": time.time()}
    task = asyncio.create_task(_run_pull(row["address"], name))
    _pull_tasks.add(task)
    task.add_done_callback(_pull_tasks.discard)
    return PULLS[name]


@router.get("/pulls")
def pulls():
    cutoff = time.time() - 3600
    for name in [n for n, s in PULLS.items() if s.get("finished_at", time.time()) < cutoff]:
        PULLS.pop(name, None)
    return list(PULLS.values())


# --- Advanced settings -----------------------------------------------------------


def _model_for(name: str):
    row = _connected_or_409()
    with db.session() as conn:
        m = conn.execute("SELECT * FROM models WHERE server_id = ? AND name = ?", (row["id"], name)).fetchone()
        if m is None:
            raise HTTPException(404, "That model isn't known on this server.")
        s = conn.execute("SELECT settings FROM model_settings WHERE server_id = ? AND name = ?", (row["id"], name)).fetchone()
    return row, ai_servers.model_from_row(m), (json.loads(s["settings"]) if s else {})


@router.get("/advanced")
def get_advanced(name: str):
    _, model, current = _model_for(name)
    return {"name": name, "options": advanced.options_for(model), "settings": current,
            "warnings": advanced.warnings_for(model, current)}


@router.put("/advanced")
def put_advanced(name: str, body: dict):
    row, model, _ = _model_for(name)
    try:
        clean = advanced.validate(model, body)
    except advanced.AdvancedError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db.session() as conn:
        if clean:
            conn.execute(
                "INSERT INTO model_settings (server_id, name, settings) VALUES (?, ?, ?) "
                "ON CONFLICT(server_id, name) DO UPDATE SET settings = excluded.settings",
                (row["id"], name, json.dumps(clean)),
            )
        else:
            conn.execute("DELETE FROM model_settings WHERE server_id = ? AND name = ?", (row["id"], name))
    return {"name": name, "settings": clean, "warnings": advanced.warnings_for(model, clean)}


@router.delete("/advanced")
def reset_advanced(name: str):
    row, _, _ = _model_for(name)
    with db.session() as conn:
        conn.execute("DELETE FROM model_settings WHERE server_id = ? AND name = ?", (row["id"], name))
    return {"name": name, "settings": {}}
