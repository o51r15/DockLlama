"""Machines and the containers on them (Settings → Machines / Containers)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, machines

router = APIRouter(prefix="/api", tags=["containers"])

MAX_NOTES = 4000


async def _refresh(machine_id: int) -> tuple[list[machines.ContainerInfo] | None, str | None, str | None]:
    """Ask Docker for the machine's containers and record them. Returns (infos, version, error)."""
    m = machines.get_machine(machine_id)
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
        "model_override": row["model_override"],
        "allow_auto_restart": bool(row["allow_auto_restart"]),
        "ignore_patterns": json.loads(row["ignore_patterns"]),
        "known_patterns": json.loads(row["known_patterns"]),
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
    result = []
    for row in rows:
        infos, version, error = await _refresh(row["id"])
        result.append({
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "connected": error is None,
            "error": error,
            "docker_version": version,
            "container_count": len(infos) if infos is not None else None,
        })
    return result


@router.get("/machines/{machine_id}/containers")
async def list_containers(machine_id: int):
    try:
        machines.get_machine(machine_id)
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
    model_override: str | None = Field(default=None, max_length=200)  # "" = use the main model


@router.patch("/containers/{db_id}")
def update_container(db_id: int, body: ContainerUpdate):
    changes = body.model_dump(exclude_none=True)
    if "restart_group" in changes:
        changes["restart_group"] = changes["restart_group"].strip().lower()  # "" = not in a group
    if "model_override" in changes:
        changes["model_override"] = changes["model_override"].strip() or None
    if not changes:
        raise HTTPException(400, "Nothing to change.")
    with db.session() as conn, db.transaction(conn):
        if conn.execute("SELECT 1 FROM containers WHERE id = ?", (db_id,)).fetchone() is None:
            raise HTTPException(404, "That container isn't known to DockLlama.")
        for column, value in changes.items():  # column names come from the model, not the user
            conn.execute(f"UPDATE containers SET {column} = ? WHERE id = ?", (value, db_id))
        row = conn.execute("SELECT * FROM containers WHERE id = ?", (db_id,)).fetchone()
    return {"db_id": db_id, **_row_settings(row)}


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
