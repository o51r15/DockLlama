import re

import pytest

from dockllama import db, machines
from dockllama.machines import ContainerInfo, MachineError


def info(name, **kw):
    base = dict(
        id=name * 3 + "0" * 40, name=name, image="nginx:latest", state="running", health=None,
        exit_code=0, oom_killed=False, restart_count=0, restart_policy="unless-stopped",
        started_at=None, finished_at=None, log_driver="json-file", compose_project="web",
        compose_service=name, is_self=False, is_ollama=False,
    )
    base.update(kw)
    return ContainerInfo(**base)


class FakeDocker:
    def __init__(self, infos=None, error=None):
        self.infos, self.error = infos or [], error

    async def aversion(self):
        if self.error:
            raise MachineError(self.error)
        return "29.0.0"

    async def alist_containers(self):
        if self.error:
            raise MachineError(self.error)
        return list(self.infos)


@pytest.fixture
def fake(monkeypatch):
    f = FakeDocker([info("web"), info("db", image="postgres:16")])
    monkeypatch.setattr(machines, "_local", f)
    return f


@pytest.fixture
def signed_in(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    return client


# --- plain-language states ---------------------------------------------------

@pytest.mark.parametrize("kw,text", [
    (dict(state="running"), "Running"),
    (dict(state="running", health="unhealthy"), "Running, failing its health check"),
    (dict(state="running", health="starting"), "Starting up"),
    (dict(state="exited", exit_code=0), "Stopped"),
    (dict(state="exited", exit_code=137), "Stopped with an error (code 137)"),
    (dict(state="exited", exit_code=137, oom_killed=True), "Stopped: ran out of memory"),
    (dict(state="restarting"), "Restarting"),
    (dict(state="created"), "Created but never started"),
])
def test_describe_state(kw, text):
    assert machines.describe_state(info("x", **kw)) == text


@pytest.mark.parametrize("image,expected", [
    ("ollama/ollama", True), ("ollama/ollama:0.40.17", True), ("ollama/ollama:rocm", True),
    ("ghcr.io/someone/ollama@sha256:abc", True), ("docker.io/ollama/ollama:latest", True),
    ("ollama-webui", False), ("ghcr.io/open-webui/open-webui:ollama", False), ("nginx", False),
])
def test_is_ollama(image, expected):
    assert machines._is_ollama(image) is expected


def test_own_container_id_from_mountinfo(tmp_path, monkeypatch):
    cid = "a" * 64
    fake_mountinfo = tmp_path / "mountinfo"
    fake_mountinfo.write_text(f"1 2 0:1 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw\n")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(fake_mountinfo if p == "/proc/self/mountinfo" else p, *a, **k))
    machines.own_container_id.cache_clear()
    try:
        assert machines.own_container_id() == cid
        assert machines._is_self(cid) and machines._is_self(cid[:12]) and not machines._is_self("b" * 64)
    finally:
        machines.own_container_id.cache_clear()


def test_missing_socket_gives_plain_message(tmp_path):
    with pytest.raises(MachineError, match=re.escape("compose file needs the /var/run/docker.sock")):
        machines.LocalDocker(str(tmp_path / "nope.sock")).list_containers()


# --- syncing with the database -----------------------------------------------

def test_sync_marks_gone_and_revives(conn):
    machines.sync_containers(conn, 1, [info("a"), info("b")])
    machines.sync_containers(conn, 1, [info("a")])
    gone = {r["name"]: r["gone"] for r in conn.execute("SELECT name, gone FROM containers")}
    assert gone == {"a": 0, "b": 1}
    machines.sync_containers(conn, 1, [info("a"), info("b")])
    assert conn.execute("SELECT gone FROM containers WHERE name='b'").fetchone()["gone"] == 0


def test_sync_with_no_containers_marks_all_gone(conn):
    machines.sync_containers(conn, 1, [info("a")])
    machines.sync_containers(conn, 1, [])
    assert conn.execute("SELECT gone FROM containers WHERE name='a'").fetchone()["gone"] == 1


# --- API ------------------------------------------------------------------------

def test_container_endpoints_need_sign_in(client, fake):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    client.cookies.clear()
    assert client.get("/api/machines/1/containers").status_code == 401
    assert client.get("/settings", follow_redirects=False).headers["location"] == "login"


def test_list_and_watch(signed_in, fake):
    d = signed_in.get("/api/machines/1/containers").json()
    assert d["error"] is None
    by_name = {c["name"]: c for c in d["containers"]}
    assert set(by_name) == {"web", "db"}
    assert by_name["web"]["monitored"] is False and by_name["web"]["state_text"] == "Running"
    r = signed_in.patch(f"/api/containers/{by_name['web']['db_id']}", json={"monitored": True, "notes_for_ai": "VPN; reconnects are normal"})
    assert r.status_code == 200 and r.json()["monitored"] is True
    again = {c["name"]: c for c in signed_in.get("/api/machines/1/containers").json()["containers"]}
    assert again["web"]["monitored"] is True and again["web"]["notes_for_ai"] == "VPN; reconnects are normal"
    assert again["db"]["monitored"] is False


def test_bulk_watch(signed_in, fake):
    items = signed_in.get("/api/machines/1/containers").json()["containers"]
    ids = [c["db_id"] for c in items]
    assert signed_in.post("/api/containers/monitor", json={"db_ids": ids, "monitored": True}).json()["count"] == 2
    assert all(c["monitored"] for c in signed_in.get("/api/machines/1/containers").json()["containers"])


def test_gone_container_listed_and_removable(signed_in, fake):
    items = {c["name"]: c for c in signed_in.get("/api/machines/1/containers").json()["containers"]}
    live_id, gone_id = items["web"]["db_id"], items["db"]["db_id"]
    assert signed_in.delete(f"/api/containers/{live_id}").status_code == 409  # still exists
    fake.infos = [info("web")]
    items = {c["name"]: c for c in signed_in.get("/api/machines/1/containers").json()["containers"]}
    assert items["db"]["gone"] is True and items["db"]["state_text"] == "No longer exists"
    assert signed_in.delete(f"/api/containers/{gone_id}").status_code == 200
    names = [c["name"] for c in signed_in.get("/api/machines/1/containers").json()["containers"]]
    assert names == ["web"]


def test_docker_error_is_reported_and_nothing_marked_gone(signed_in, fake):
    signed_in.get("/api/machines/1/containers")
    fake.error = "DockLlama can't see Docker on this machine."
    d = signed_in.get("/api/machines/1/containers").json()
    assert d["error"] == "DockLlama can't see Docker on this machine." and d["containers"] == []
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM containers WHERE gone = 1").fetchone()["n"] == 0
    m = signed_in.get("/api/machines").json()[0]
    assert m["connected"] is False and "can't see Docker" in m["error"]


def test_update_validation(signed_in, fake):
    items = signed_in.get("/api/machines/1/containers").json()["containers"]
    cid = items[0]["db_id"]
    assert signed_in.patch(f"/api/containers/{cid}", json={}).status_code == 400
    assert signed_in.patch(f"/api/containers/{cid}", json={"notes_for_ai": "x" * 5000}).status_code == 422
    assert signed_in.patch("/api/containers/9999", json={"monitored": True}).status_code == 404
