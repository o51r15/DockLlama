import asyncio
import json

import httpx
import pytest

from dockllama import checks, db, machines, remote, restarts, settings

from .test_checks import FakeDocker

TOKEN = "t" * 32


class FakeLocal(FakeDocker):
    """Remote's side: the blocking LocalDocker methods, backed by the async fake."""
    socket_path = "/nonexistent"
    restarted: list

    def version(self):
        return "29.0.0"

    def list_containers(self):
        return asyncio.run(self.alist_containers())

    def inspect(self, name):
        a = asyncio.run(self.ainspect(name))
        a["Config"]["Env"] = ["DB_PASSWORD=hunter2"]
        a["Mounts"] = [{"Source": "/secret"}]
        return a

    def logs(self, name, since, tail):
        return asyncio.run(self.alogs(name, since, tail))

    def stats(self, name):
        return 4.0, 30.0

    def events(self, cid, since, until):
        return self.events_by_id.get(cid, [])

    def restart(self, name, timeout=10):
        self.restarted = [*getattr(self, "restarted", []), name]

    def start_event_watch(self):
        pass


@pytest.fixture
def far(monkeypatch):
    """A Docker machine with DockLlama Remote on it, reachable in-process."""
    monkeypatch.setenv("DOCKLLAMA_REMOTE_TOKEN", TOKEN)
    d = FakeLocal()
    d.add("nas-app"); d.log("nas-app", "INFO ok")
    d.add("nas-db", "exited", 1)
    app = remote.create_app(d)
    monkeypatch.setattr(machines, "REMOTE_TRANSPORT", httpx.ASGITransport(app=app))
    return d


def test_remote_api_is_locked_and_hides_secrets(far):
    async def go():
        r = machines.RemoteDocker("http://nas:8557", TOKEN, "nas")
        info = await r.ainfo()
        assert info["allows_restart"] is False and info["docker_version"] == "29.0.0"
        names = [c.name for c in await r.alist_containers()]
        assert names == ["nas-app", "nas-db"]
        a = await r.ainspect("nas-app")
        assert "Env" not in a["Config"] and "Mounts" not in a and a["State"]["Status"] == "running"
        assert "hunter2" not in json.dumps(a)
        assert [t for _, t in await r.alogs("nas-app", None, 10)] == ["INFO ok"]
        assert await r.astats("nas-app") == (4.0, 30.0)
        with pytest.raises(machines.MachineError, match="doesn't allow restarts"):
            await r.arestart("nas-app")
        with pytest.raises(machines.MachineError, match="pairing token"):
            await machines.RemoteDocker("http://nas:8557", "x" * 32, "nas").ainfo()
        with pytest.raises(machines.MachineError, match="doesn't exist"):
            await r.ainspect("nope")
    asyncio.run(go())


def test_remote_restarts_when_allowed(far, monkeypatch):
    monkeypatch.setenv("DOCKLLAMA_REMOTE_RESTARTS", "yes")
    asyncio.run(machines.RemoteDocker("http://nas:8557", TOKEN).arestart("nas-app"))
    assert far.restarted == ["nas-app"]


def test_remote_refuses_to_start_without_a_token(monkeypatch, capsys):
    monkeypatch.setenv("DOCKLLAMA_REMOTE_TOKEN", "short")
    assert remote.serve() == 2 and "DOCKLLAMA_REMOTE_TOKEN" in capsys.readouterr().err


def test_remote_url():
    assert machines.remote_url("192.168.1.20") == "http://192.168.1.20:8557"
    assert machines.remote_url("http://nas.lan:9000/") == "http://nas.lan:9000"
    with pytest.raises(machines.MachineError):
        machines.remote_url("  ")


def test_add_a_machine_and_check_its_containers(client, far, monkeypatch):
    local = FakeDocker(); local.add("web"); local.log("web", "INFO hi")
    real_get = machines.get_machine
    monkeypatch.setattr(machines, "get_machine", lambda mid: local if mid == 1 else real_get(mid))
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    snip = client.post("/api/machines/remote-snippet", json={"allow_restarts": True}).json()
    assert snip["token"] in snip["compose"] and 'DOCKLLAMA_REMOTE_RESTARTS: "yes"' in snip["compose"] \
        and '["dockllama", "remote"]' in snip["compose"]
    bad = client.post("/api/machines", json={"name": "NAS", "address": "nas", "token": "y" * 32}).json()
    assert bad["ok"] is False and "pairing token" in bad["problem"]
    r = client.post("/api/machines", json={"name": "NAS", "address": "nas", "token": TOKEN}).json()
    assert r["ok"] and r["containers"] == 2
    assert client.post("/api/machines", json={"name": "NAS2", "address": "nas", "token": TOKEN}).status_code == 409
    ms = client.get("/api/machines").json()
    assert [(m["name"], m["connected"]) for m in ms] == [("This machine", True), ("NAS", True)]
    # same version as DockLlama: no "update your Remote" note
    assert ms[1]["remote_version"] == ms[1]["version"] and ms[1]["remote_older"] is False and ms[0]["remote_older"] is False
    import dockllama
    monkeypatch.setattr(dockllama, "__version__", "99.0.0")  # DockLlama is newer than the Remote
    assert client.get("/api/machines").json()[1]["remote_older"] is True
    monkeypatch.setattr(dockllama, "__version__", ms[1]["version"])
    cs = client.get(f"/api/machines/{r['id']}/containers").json()["containers"]
    assert {c["name"] for c in cs} == {"nas-app", "nas-db"}
    with db.session() as conn:
        conn.execute("UPDATE containers SET monitored = 1")
    results = {x["name"]: x for x in asyncio.run(checks.run_checks())}
    assert results["nas-db"]["status"] == "critical" and results["web"]["status"] == "healthy"
    assert results["nas-app"]["machine_id"] == r["id"]
    # the machine goes away: only its containers become "Couldn't check"
    monkeypatch.setattr(machines, "REMOTE_TRANSPORT", httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        httpx.ConnectError("down", request=req))))
    results = {x["name"]: x for x in asyncio.run(checks.run_checks())}
    assert results["nas-app"]["status"] == "unknown" and "Can't reach DockLlama Remote on NAS" in results["nas-app"]["summary"]
    assert results["web"]["status"] == "healthy"
    d = client.get("/api/dashboard").json()
    assert {c["machine"] for c in d["containers"]} == {"This machine", "NAS"}
    assert client.delete(f"/api/machines/{r['id']}").json()["ok"]
    assert client.delete("/api/machines/1").status_code == 400


def test_restart_respects_the_machine(conn):
    conn.execute("INSERT INTO machines (id, name, kind, url, token, allows_restart) VALUES (2, 'NAS', 'remote', 'http://nas:8557', 't', 0)")
    conn.execute("INSERT INTO containers (id, machine_id, name, monitored, allow_auto_restart, first_seen, last_seen) "
                 "VALUES (1, 2, 'nas-app', 1, 1, 0, 0)")
    settings.set(conn, "fixing.mode", "auto")
    assert restarts.blocked(conn, 1, 0) == "machine_says_no"
    conn.execute("UPDATE machines SET allows_restart = 1 WHERE id = 2")
    assert restarts.blocked(conn, 1, 0) is None


def test_container_page_explains_why_restart_is_off(client, far):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    r = client.post("/api/machines", json={"name": "NAS", "address": "nas", "token": TOKEN}).json()
    with db.session() as conn:
        cid = conn.execute("SELECT id FROM containers WHERE machine_id = ? AND name = 'nas-app'", (r["id"],)).fetchone()["id"]
    d = client.get(f"/api/containers/{cid}/detail").json()
    assert d["can_restart"] is False and "NAS doesn't allow restarts" in d["why_not_restart"]
