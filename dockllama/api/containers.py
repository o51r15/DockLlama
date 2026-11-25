"""Machines and the containers on them (Settings → Machines / Containers)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import __version__, db, machines, webcheck

router = APIRouter(prefix="/api", tags=["containers"])

MAX_NOTES = 4000


async def _refresh(machine_id: int, m=None) -> tuple[list[machines.ContainerInfo] | None, str | None, str | None]:
    """Ask Docker for the machine's containers and record them. Returns (infos, version, error)."""
    m = m or machines.get_machine(machine_id)
    try:
        version = await m.aversion()
        infos = await m.alist_containers()
    except machines.MachineError as exc:
        await run_in_threadpool(_save_error, machine_id, str(exc))
        return None, None, str(exc)
    await run_in_threadpool(_save_sync, machine_id, infos)
    return infos, version, None


def _save_error(machine_id: int, message: str) -> None:
    with db.session() as conn:
        machines.record_machine_error(conn, machine_id, message)


def _save_sync(machine_id: int, infos) -> None:
    with db.session() as conn, db.transaction(conn):
        machines.sync_containers(conn, machine_id, infos)


def _row_settings(row) -> dict:
    return {
        "monitored": bool(row["monitored"]),
        "notes_for_ai": row["notes_for_ai"],
        "restart_group": row["restart_group"],
        "restart_order": row["restart_order"],
        "allow_auto_restart": bool(row["allow_auto_restart"]),
        "ignore_patterns": json.loads(row["ignore_patterns"]),
        "known_patterns": json.loads(row["known_patterns"]),
        "web_url": row["web_url"],
        "web_word": row["web_word"],
        "web_fails": row["web_fails"],
        "web_last_ok": row["web_last_ok"],
        "web_last_error": row["web_last_error"],
    }


def _machine_rows():
    with db.session() as conn:
        return conn.execute("SELECT * FROM machines ORDER BY id").fetchall()


def _container_rows(machine_id: int) -> dict:
    with db.session() as conn:
        return {
            r["name"]: r
            for r in conn.execute("SELECT * FROM containers WHERE machine_id = ?", (machine_id,))
        }


@router.get("/machines")
async def list_machines():
    rows = await run_in_threadpool(_machine_rows)

    async def one(row):
        m = machines.get_machine(row["id"])
        infos, version, error = await _refresh(row["id"], m)
        remote_version = getattr(m, "remote_version", None) if row["kind"] == "remote" else None
        older = row["kind"] == "remote" and error is None and machines.remote_is_older(remote_version)
        return {
            "remote_version": remote_version, "remote_older": older, "version": __version__,
            "id": row["id"], "name": row["name"], "kind": row["kind"], "url": row["url"] if row["kind"] == "remote" else None,
            "allows_restart": bool(row["allows_restart"]),
            "connected": error is None, "error": error, "docker_version": version,
            "container_count": len(infos) if infos is not None else None,
        }
    import asyncio
    return list(await asyncio.gather(*(one(r) for r in rows)))


REMOTE_IMAGE = "ghcr.io/o51r15/dockllama:latest"


class SnippetBody(BaseModel):
    allow_restarts: bool = False


@router.post("/machines/remote-snippet")
def remote_snippet(body: SnippetBody):
    """A fresh pairing token and the compose file to run on the other machine."""
    import secrets
    token = secrets.token_urlsafe(32)
    yaml = "\n".join([
        "services:",
        "  dockllama-remote:",
        f"    image: {REMOTE_IMAGE}",
        "    container_name: dockllama-remote",
        '    command: ["dockllama", "remote"]',
        "    restart: unless-stopped",
        "    ports:",
        f'      - "{machines.REMOTE_PORT}:{machines.REMOTE_PORT}"',
        "    volumes:",
        "      - /var/run/docker.sock:/var/run/docker.sock",
        "    environment:",
        f'      DOCKLLAMA_REMOTE_TOKEN: "{token}"',
        f'      DOCKLLAMA_REMOTE_RESTARTS: "{"yes" if body.allow_restarts else "no"}"',
        "",
    ])
    return {"token": token, "compose": yaml}


class AddMachineBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    address: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=24, max_length=200)


@router.post("/machines")
async def add_machine(body: AddMachineBody):
    try:
        url = machines.remote_url(body.address)
    except machines.MachineError as exc:
        raise HTTPException(400, str(exc)) from exc
    remote = machines.RemoteDocker(url, body.token.strip(), body.name.strip())
    try:
        info = await remote.ainfo()
        infos = await remote.alist_containers()
    except machines.MachineError as exc:
        return {"ok": False, "problem": str(exc)}

    def save():
        with db.session() as conn, db.transaction(conn):
            if conn.execute("SELECT 1 FROM machines WHERE url = ?", (url,)).fetchone():
                raise HTTPException(409, f"{url} is already added.")
            mid = conn.execute("INSERT INTO machines (name, kind, url, token, allows_restart, last_seen) "
                               "VALUES (?, 'remote', ?, ?, ?, strftime('%s','now'))",
                               (body.name.strip(), url, body.token.strip(), int(bool(info.get("allows_restart"))))).lastrowid
            machines.sync_containers(conn, mid, infos)
            return mid
    mid = await run_in_threadpool(save)
    return {"ok": True, "id": mid, "containers": len(infos), "allows_restart": bool(info.get("allows_restart")),
            "remote_version": info.get("version")}


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.patch("/machines/{machine_id}")
def rename_machine(machine_id: int, body: RenameBody):
    with db.session() as conn:
        if conn.execute("UPDATE machines SET name = ? WHERE id = ?", (body.name.strip(), machine_id)).rowcount == 0:
            raise HTTPException(404, "That machine isn't known.")
    return {"ok": True}


@router.delete("/machines/{machine_id}")
def remove_machine(machine_id: int):
    if machine_id == 1:
        raise HTTPException(400, "This machine can't be removed.")
    with db.session() as conn, db.transaction(conn):
        conn.execute("DELETE FROM containers WHERE machine_id = ?", (machine_id,))
        conn.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
    return {"ok": True}


@router.get("/machines/{machine_id}/containers")
async def list_containers(machine_id: int):
    try:
        await run_in_threadpool(machines.get_machine, machine_id)
    except machines.MachineError as exc:
        raise HTTPException(404, str(exc)) from exc
    infos, _, error = await _refresh(machine_id)
    rows = await run_in_threadpool(_container_rows, machine_id)
    items = []
    for info in infos or []:
        row = rows.get(info.name)
        item = info.to_dict()
        item["db_id"] = row["id"] if row else None
        item["gone"] = False
        item.update(_row_settings(row) if row else {})
        items.append(item)
    if infos is not None:  # only report "gone" when we actually reached Docker
        for name, row in rows.items():
            if row["gone"]:
                items.append({"db_id": row["id"], "name": name, "gone": True, "state": "gone",
                              "state_text": "No longer exists", **_row_settings(row)})
    return {"error": error, "containers": items}


class ContainerUpdate(BaseModel):
    monitored: bool | None = None
    notes_for_ai: str | None = Field(default=None, max_length=MAX_NOTES)
    allow_auto_restart: bool | None = None
    restart_group: str | None = Field(default=None, max_length=40)  # "" = not in a group
    restart_order: int | None = Field(default=None, ge=0, le=99)
    web_url: str | None = Field(default=None, max_length=500)  # "" = no web page check
    web_word: str | None = Field(default=None, max_length=100)


@router.patch("/containers/{db_id}")
def update_container(db_id: int, body: ContainerUpdate):
    changes = body.model_dump(exclude_none=True)
    if "web_url" in changes:
        changes["web_url"] = changes["web_url"].strip() or None
        if changes["web_url"]:
            problem = webcheck.address_problem(changes["web_url"])
            if problem:
                raise HTTPException(400, problem)
        changes["web_fails"] = 0  # a new address starts with a clean slate
        changes["web_last_error"] = None
    if "web_word" in changes:
        changes["web_word"] = changes["web_word"].strip()
    if "restart_group" in changes:
        changes["restart_group"] = changes["restart_group"].strip().lower()  # "" = not in a group
    if not changes:
        raise HTTPException(400, "Nothing to change.")
    with db.session() as conn, db.transaction(conn):
        if conn.execute("SELECT 1 FROM containers WHERE id = ?", (db_id,)).fetchone() is None:
            raise HTTPException(404, "That container isn't known to DockLlama.")
        for column, value in changes.items():  # column names come from the model, not the user
            conn.execute(f"UPDATE containers SET {column} = ? WHERE id = ?", (value, db_id))
        row = conn.execute("SELECT * FROM containers WHERE id = ?", (db_id,)).fetchone()
    return {"db_id": db_id, **_row_settings(row)}


@router.get("/containers/{db_id}/web-check")
async def web_check_info(db_id: int):
    """The current web page check, plus addresses to suggest from the container's published ports."""
    def read():
        with db.session() as conn:
            row = conn.execute("SELECT c.*, m.url AS machine_url, m.kind FROM containers c JOIN machines m "
                               "ON m.id = c.machine_id WHERE c.id = ?", (db_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "That container isn't known to DockLlama.")
        return dict(row)
    row = await run_in_threadpool(read)
    suggested: list[str] = []
    try:
        machine = await run_in_threadpool(machines.get_machine, row["machine_id"])
        inspect = await machine.ainspect(row["name"])
        suggested = webcheck.suggestions(inspect, row["machine_url"] if row["kind"] == "remote" else None)
    except machines.MachineError:
        pass
    return {"web_url": row["web_url"], "web_word": row["web_word"], "web_fails": row["web_fails"],
            "web_last_ok": row["web_last_ok"], "web_last_error": row["web_last_error"],
            "suggested": suggested, "fails_needed": webcheck.FAILS_NEEDED}


class WebTest(BaseModel):
    url: str = Field(max_length=500)
    word: str = Field(default="", max_length=100)


@router.post("/containers/{db_id}/web-check/test")
async def web_check_test(db_id: int, body: WebTest):
    """Try an address once, before saving it."""
    problem = webcheck.address_problem(body.url)
    if problem:
        return {"ok": False, "detail": problem}
    ok, detail = await webcheck.check(body.url.strip(), body.word)
    return {"ok": ok, "detail": detail}


class BulkMonitor(BaseModel):
    db_ids: list[int]
    monitored: bool


@router.post("/containers/monitor")
def bulk_monitor(body: BulkMonitor):
    with db.session() as conn, db.transaction(conn):
        for db_id in body.db_ids:
            conn.execute("UPDATE containers SET monitored = ? WHERE id = ?", (body.monitored, db_id))
    return {"ok": True, "count": len(body.db_ids)}


@router.delete("/containers/{db_id}")
def forget_container(db_id: int):
    """Remove a container that no longer exists, with its settings."""
    with db.session() as conn, db.transaction(conn):
        row = conn.execute("SELECT gone FROM containers WHERE id = ?", (db_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "That container isn't known to DockLlama.")
        if not row["gone"]:
            raise HTTPException(409, "That container still exists. Turn off watching instead.")
        conn.execute("DELETE FROM containers WHERE id = ?", (db_id,))
    return {"ok": True}
