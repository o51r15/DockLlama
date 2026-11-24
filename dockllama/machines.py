"""Talking to Docker on each machine.

Phase 2 covers "This machine" (the local socket). Remote machines (DockLlama Remote) come in
phase 9 behind the same interface. The Docker SDK is blocking, so callers use the async
wrappers, which run it in a worker thread.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from functools import lru_cache

import docker
from docker.errors import DockerException

SOCKET_PATH = "/var/run/docker.sock"


class MachineError(Exception):
    """A problem reaching Docker, worded for the user."""


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    state: str  # running | exited | restarting | paused | created | dead | removing
    health: str | None  # healthy | unhealthy | starting | None (no healthcheck)
    exit_code: int | None
    oom_killed: bool
    restart_count: int
    restart_policy: str
    started_at: str | None
    finished_at: str | None
    log_driver: str
    compose_project: str | None
    compose_service: str | None
    is_self: bool
    is_ollama: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state_text"] = describe_state(self)
        d["logs_readable"] = self.log_driver in READABLE_LOG_DRIVERS
        return d


# Drivers `docker logs` can read back. Others (none, syslog, gelf, fluentd, awslogs, splunk…)
# can't, so DockLlama can still watch the container's state but not what it says.
READABLE_LOG_DRIVERS = {"json-file", "local", "journald", "etwlogs"}


def describe_state(c: ContainerInfo) -> str:
    """Docker's state in plain words."""
    if c.state == "running":
        if c.health == "unhealthy":
            return "Running, failing its health check"
        if c.health == "starting":
            return "Starting up"
        return "Running"
    if c.state == "exited":
        if c.oom_killed:
            return "Stopped: ran out of memory"
        if c.exit_code in (0, None):
            return "Stopped"
        return f"Stopped with an error (code {c.exit_code})"
    return {
        "restarting": "Restarting",
        "paused": "Paused",
        "created": "Created but never started",
        "dead": "Dead (Docker couldn't stop or remove it cleanly)",
        "removing": "Being removed",
    }.get(c.state, c.state.capitalize())


@lru_cache(maxsize=1)
def own_container_id() -> str | None:
    """This container's full ID, so DockLlama can leave itself out by default."""
    try:
        with open("/proc/self/mountinfo") as f:
            m = re.search(r"/containers/([0-9a-f]{64})/", f.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    host = os.environ.get("HOSTNAME", "")
    return host if re.fullmatch(r"[0-9a-f]{12}", host) else None


def _is_self(container_id: str) -> bool:
    own = own_container_id()
    return bool(own) and (container_id.startswith(own) or own.startswith(container_id))


def _is_ollama(image: str) -> bool:
    """True for ollama/ollama, ollama/ollama:rocm, ghcr.io/x/ollama@sha256:…, etc."""
    last = image.split("@")[0].split("/")[-1]  # drop digest and registry/namespace
    return last.split(":")[0] == "ollama"  # drop tag


def _info(c) -> ContainerInfo:
    a = c.attrs
    state = a.get("State", {})
    labels = (a.get("Config") or {}).get("Labels") or {}
    image = (a.get("Config") or {}).get("Image") or ""
    return ContainerInfo(
        id=a["Id"],
        name=a.get("Name", "").lstrip("/"),
        image=image,
        state=state.get("Status", "unknown"),
        health=(state.get("Health") or {}).get("Status"),
        exit_code=state.get("ExitCode"),
        oom_killed=bool(state.get("OOMKilled")),
        restart_count=int(a.get("RestartCount") or 0),
        restart_policy=((a.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no",
        started_at=state.get("StartedAt"),
        finished_at=state.get("FinishedAt"),
        log_driver=((a.get("HostConfig") or {}).get("LogConfig") or {}).get("Type") or "json-file",
        compose_project=labels.get("com.docker.compose.project"),
        compose_service=labels.get("com.docker.compose.service"),
        is_self=_is_self(a["Id"]),
        is_ollama=_is_ollama(image),
    )


class LocalDocker:
    """Docker on the machine DockLlama runs on, through the mounted socket."""

    def __init__(self, socket_path: str = SOCKET_PATH):
        self.socket_path = socket_path
        self._client: docker.DockerClient | None = None

    def _connect(self) -> docker.DockerClient:
        if not os.path.exists(self.socket_path):
            raise MachineError(
                "DockLlama can't see Docker on this machine. Your compose file needs the "
                "/var/run/docker.sock:/var/run/docker.sock line under volumes."
            )
        if not os.access(self.socket_path, os.R_OK | os.W_OK):
            raise MachineError(
                "DockLlama found Docker but isn't allowed to use it. If you set a user: line "
                "in your compose file, remove it (use PUID/PGID instead). Rootless Docker and "
                "Podman aren't supported yet."
            )
        if self._client is None:
            try:
                self._client = docker.DockerClient(base_url=f"unix://{self.socket_path}", timeout=20)
            except DockerException as exc:
                raise MachineError(f"DockLlama couldn't connect to Docker: {exc}") from exc
        return self._client

    def version(self) -> str:
        try:
            return self._connect().version().get("Version", "unknown")
        except MachineError:
            raise
        except Exception as exc:  # noqa: BLE001 - any Docker/HTTP failure becomes a user message
            self._client = None
            raise MachineError(f"Docker didn't answer: {exc}") from exc

    def list_containers(self) -> list[ContainerInfo]:
        try:
            raw = self._connect().containers.list(all=True)
        except MachineError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._client = None
            raise MachineError(f"Docker didn't answer: {exc}") from exc
        result = []
        for c in raw:
            try:
                result.append(_info(c))
            except Exception:  # noqa: BLE001 - a container removed mid-listing; skip it
                continue
        return sorted(result, key=lambda i: ((i.compose_project or "~"), i.name))

    # Async wrappers: the SDK blocks, so it runs in a worker thread.
    async def aversion(self) -> str:
        return await asyncio.to_thread(self.version)

    async def alist_containers(self) -> list[ContainerInfo]:
        return await asyncio.to_thread(self.list_containers)


_local = LocalDocker()


def get_machine(machine_id: int) -> LocalDocker:
    if machine_id != 1:
        raise MachineError("Only this machine is supported so far.")
    return _local


# --- keeping the containers table in step with Docker -------------------------


def sync_containers(conn: sqlite3.Connection, machine_id: int, infos: list[ContainerInfo]) -> None:
    """Record every container seen; mark ones that disappeared as gone (never deleted here)."""
    now = time.time()
    names = [i.name for i in infos]
    for name in names:
        conn.execute(
            "INSERT INTO containers (machine_id, name, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(machine_id, name) DO UPDATE SET last_seen = excluded.last_seen, gone = 0",
            (machine_id, name, now, now),
        )
    placeholders = ",".join("?" * len(names)) or "''"
    conn.execute(
        f"UPDATE containers SET gone = 1 WHERE machine_id = ? AND name NOT IN ({placeholders})",
        (machine_id, *names),
    )
    conn.execute(
        "UPDATE machines SET last_seen = ?, last_error = NULL WHERE id = ?", (now, machine_id)
    )


def record_machine_error(conn: sqlite3.Connection, machine_id: int, message: str) -> None:
    conn.execute("UPDATE machines SET last_error = ? WHERE id = ?", (message, machine_id))
