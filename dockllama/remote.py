"""DockLlama Remote: a small helper for watching another Docker machine (plan section 4, 9).

Run on the other machine as `dockllama remote` (same image). The main DockLlama calls it over the
LAN with a pairing token. It can only: describe itself, list containers, read a container's state
(never its environment variables, which often hold secrets), logs, stats and Docker events, and
restart a container if this machine allows it. No AI, no web page, no database.

Settings (environment variables, shown in the snippet Settings → Machines makes for you):
  DOCKLLAMA_REMOTE_TOKEN     the pairing token (required, at least 24 characters)
  DOCKLLAMA_REMOTE_RESTARTS  "yes" to let DockLlama restart containers here (default "no")
  DOCKLLAMA_REMOTE_PORT      port to listen on (default 8557)
"""

from __future__ import annotations

import hmac
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from . import __version__, machines

PORT = int(os.environ.get("DOCKLLAMA_REMOTE_PORT", "8557"))
SAFE_INSPECT_KEYS = ("Id", "Name", "RestartCount", "Created")
SAFE_STATE_KEYS = ("Status", "Running", "Paused", "Restarting", "OOMKilled", "Dead", "ExitCode", "Error",
                   "StartedAt", "FinishedAt", "Health")


def token() -> str:
    return os.environ.get("DOCKLLAMA_REMOTE_TOKEN", "")


def restarts_allowed() -> bool:
    return os.environ.get("DOCKLLAMA_REMOTE_RESTARTS", "no").strip().lower() in ("yes", "true", "1", "on")


def safe_inspect(a: dict) -> dict:
    """Only what DockLlama needs. Config.Env, mounts, networks and the rest stay on this machine."""
    out = {k: a.get(k) for k in SAFE_INSPECT_KEYS}
    out["State"] = {k: (a.get("State") or {}).get(k) for k in SAFE_STATE_KEYS}
    cfg, host = a.get("Config") or {}, a.get("HostConfig") or {}
    out["Config"] = {"Image": cfg.get("Image"), "Labels": {k: v for k, v in (cfg.get("Labels") or {}).items()
                                                            if k.startswith("com.docker.compose.")}}
    out["HostConfig"] = {"RestartPolicy": host.get("RestartPolicy"), "LogConfig": {"Type": (host.get("LogConfig") or {}).get("Type")}}
    # Published ports only (no IPs, no networks): lets DockLlama suggest a web page check address.
    out["NetworkSettings"] = {"Ports": (a.get("NetworkSettings") or {}).get("Ports") or {}}
    return out


def create_app(docker: machines.LocalDocker | None = None) -> FastAPI:
    d = docker or machines.LocalDocker()

    @asynccontextmanager
    async def lifespan(_app):
        if os.path.exists(d.socket_path):
            d.start_event_watch()
        yield

    app = FastAPI(title="DockLlama Remote", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    def check_token(authorization: str = Header(default="")) -> None:
        expected = token()
        given = authorization.removeprefix("Bearer ").strip()
        if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, "The pairing token doesn't match.")

    def call(fn, *args):
        try:
            return fn(*args)
        except machines.MachineError as exc:
            code = 404 if "doesn't exist" in str(exc) else 503
            raise HTTPException(code, str(exc)) from exc

    @app.get("/remote/v1/health")
    def health():
        return {"status": "ok"}

    @app.get("/remote/v1/info", dependencies=[Depends(check_token)])
    def info():
        return {"version": __version__, "docker_version": call(d.version), "allows_restart": restarts_allowed(),
                "time": time.time()}

    @app.get("/remote/v1/containers", dependencies=[Depends(check_token)])
    def containers():
        return [c.to_dict() for c in call(d.list_containers)]

    @app.get("/remote/v1/containers/{name}/inspect", dependencies=[Depends(check_token)])
    def inspect(name: str):
        return safe_inspect(call(d.inspect, name))

    @app.get("/remote/v1/containers/{name}/logs", dependencies=[Depends(check_token)])
    def logs(name: str, since: float | None = None, tail: int = 300):
        return {"lines": call(d.logs, name, since, max(1, min(tail, 2000)))}

    @app.get("/remote/v1/containers/{name}/stats", dependencies=[Depends(check_token)])
    def stats(name: str):
        cpu, mem = d.stats(name)
        return {"cpu": cpu, "mem": mem}

    @app.get("/remote/v1/events", dependencies=[Depends(check_token)])
    def events(container_id: str, since: float, until: float):
        return {"actions": d.events(container_id, since, until)}

    @app.post("/remote/v1/containers/{name}/restart", dependencies=[Depends(check_token)])
    def restart(name: str):
        if not restarts_allowed():
            raise HTTPException(403, "This machine doesn't allow restarts (DOCKLLAMA_REMOTE_RESTARTS is not yes).")
        mine = {c.name: c for c in call(d.list_containers)}
        if name in mine and mine[name].is_self:
            raise HTTPException(409, "DockLlama Remote doesn't restart itself.")
        call(d.restart, name)
        return {"ok": True}

    return app


def serve() -> int:
    import logging
    import sys

    import uvicorn
    if len(token()) < 24:
        print("DockLlama Remote needs DOCKLLAMA_REMOTE_TOKEN (at least 24 characters). Copy the snippet from "
              "DockLlama → Settings → Machines → Add another machine.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT, log_level="warning")
    return 0
