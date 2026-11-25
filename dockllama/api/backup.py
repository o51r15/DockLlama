"""Settings → Backup: automatic and manual backups, download, upload, restore."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import backup, db, model_tests, paths, settings

router = APIRouter(prefix="/api/backup", tags=["backup"])

MAX_UPLOAD = 2 * 1024 ** 3  # 2 GB


def _guard(fn, *args):
    try:
        return fn(*args)
    except backup.BackupError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def overview():
    def read():
        with db.session() as conn:
            s = {"auto": settings.get(conn, "backup.auto"), "every_hours": settings.get(conn, "backup.every_hours"),
                 "keep": settings.get(conn, "backup.keep")}
        return {**s, "folder": str(paths.USER_BACKUP_DIR), "mapped": backup.folder_is_mapped(),
                "files": backup.list_files()}
    return await run_in_threadpool(read)


class BackupSettings(BaseModel):
    auto: bool | None = None
    every_hours: int | None = None
    keep: int | None = None


@router.put("")
async def save_settings(body: BackupSettings):
    changes = body.model_dump(exclude_none=True)
    labels = {"every_hours": "How often", "keep": "How many to keep", "auto": "Automatic backups"}
    with db.session() as conn, db.transaction(conn):
        for key, value in changes.items():
            try:
                settings.set(conn, "backup." + key, value)
            except settings.SettingError as exc:
                raise HTTPException(400, f"{labels[key]} {exc}.") from exc
    return await overview()


@router.post("/now")
async def back_up_now():
    return await run_in_threadpool(_guard, backup.make, "manual")


@router.get("/files/{name}")
def download(name: str):
    p = _guard(backup._path, name)
    return FileResponse(p, media_type="application/zip", filename=name)


@router.delete("/files/{name}")
def delete(name: str):
    _guard(backup.delete_file, name)
    return {"ok": True}


@router.get("/files/{name}/contents")
async def contents(name: str):
    return await run_in_threadpool(_guard, backup.contents, name)


@router.post("/upload")
async def upload(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "That file is too big to be a DockLlama backup.")
    if not data:
        raise HTTPException(400, "Pick a backup file first.")
    name = await run_in_threadpool(_guard, backup.save_upload, bytes(data))
    return {"name": name}


class RestoreBody(BaseModel):
    groups: list[str]


@router.post("/files/{name}/restore")
async def restore(name: str, body: RestoreBody):
    def work():
        if "ai" in body.groups or "prompts" in body.groups:
            with db.session() as conn:
                if model_tests.busy_run(conn) is not None:
                    raise HTTPException(409, "A model test is running. Wait for it to finish or cancel it, "
                                             "then restore.")
        return _guard(backup.restore, name, body.groups)
    result = await run_in_threadpool(work)
    if "checking" in body.groups:
        from ..checks import SCHEDULER
        with db.session() as conn:
            SCHEDULER.reschedule(settings.get(conn, "checks.interval_minutes"))
    return result
