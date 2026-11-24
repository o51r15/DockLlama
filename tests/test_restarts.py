import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dockllama import machines, restarts, settings


@pytest.fixture
def world(conn):
    for i, (name, group, order) in enumerate([("app", "", 0), ("web", "stack", 2), ("db", "stack", 1),
                                              ("cache", "stack", 3)], 1):
        conn.execute("INSERT INTO containers (id, machine_id, name, monitored, allow_auto_restart, restart_group, "
                     "restart_order, first_seen, last_seen) VALUES (?, 1, ?, 1, 1, ?, ?, 0, 0)", (i, name, group, order))
        conn.execute("INSERT INTO container_state (container_id, status, since) VALUES (?, 'healthy', 0)", (i,))
    settings.set(conn, "fixing.mode", "auto")
    return conn


def result(cid=1, name="app", status="unhealthy", help_=True, rule=None, state="running"):
    return {"id": cid, "name": name, "status": status, "restart_would_help": help_, "rule": rule,
            "docker_state": state, "machine_id": 1}


def test_what_calls_for_a_restart():
    w = restarts.wants_restart
    assert w(result(), None) == "The AI said a restart would help."
    assert w(result(help_=False), None) is None
    assert w(result(status="degraded"), None) is None
    assert w(result(rule="crash_loop", status="critical"), None) is None  # Docker already restarts it
    assert w(result(rule="oom_loop", status="critical"), None) is None
    assert w(result(state="exited", rule="exited_error", status="critical"), None) is None
    assert w(result(rule="health_failing", help_=False), {"health_fail_checks": 2}) is None
    assert "health check failed 3" in w(result(rule="health_failing", help_=False), {"health_fail_checks": 3})


def test_watch_only_by_default(conn):
    conn.execute("INSERT INTO containers (id, machine_id, name, monitored, allow_auto_restart, first_seen, last_seen) "
                 "VALUES (1, 1, 'app', 1, 1, 0, 0)")
    assert settings.get(conn, "fixing.mode") == "watch"
    assert restarts.blocked(conn, 1, time.time()) == "watch_only"
    assert restarts.plan([result()]) == []


def test_needs_permission_cooldown_and_limit(world):
    now = time.time()
    assert restarts.blocked(world, 1, now) is None
    world.execute("UPDATE containers SET allow_auto_restart = 0 WHERE id = 1")
    assert restarts.blocked(world, 1, now) == "not_allowed"
    world.execute("UPDATE containers SET allow_auto_restart = 1 WHERE id = 1")
    world.execute("INSERT INTO events (at, container_id, kind) VALUES (?, 1, 'restart')", (now - 120,))
    assert restarts.blocked(world, 1, now) == "cooldown"
    world.execute("DELETE FROM events")
    for ago in (3000, 2000, 1000):
        world.execute("INSERT INTO events (at, container_id, kind) VALUES (?, 1, 'restart')", (now - ago,))
    assert restarts.blocked(world, 1, now) == "limit"
    assert restarts.blocked(world, 1, now + 3000) is None  # rolling 60 minutes


def test_limit_escalates_once_and_holds_for_a_day(world):
    now = time.time()
    for ago in (3000, 2000, 1000):
        world.execute("INSERT INTO events (at, container_id, kind) VALUES (?, 1, 'restart')", (now - ago,))
    assert restarts.plan([result()], now) == []
    msgs = world.execute("SELECT data FROM events WHERE kind = 'outgoing'").fetchall()
    assert len(msgs) == 1 and "restarted app 3 times in the last hour" in msgs[0]["data"]
    assert restarts.plan([result()], now + 10) == [] and len(world.execute("SELECT * FROM events WHERE kind = 'outgoing'").fetchall()) == 1
    # an hour later the window is clear, but it stays escalated until it has run fine for a day
    assert restarts.plan([result()], now + 4000) == []
    world.execute("UPDATE container_state SET status = 'healthy', since = ? WHERE container_id = 1", (now + 4000,))
    restarts.plan([], now + 4000 + 25 * 3600)
    assert world.execute("SELECT escalated_at FROM container_state WHERE container_id = 1").fetchone()["escalated_at"] is None


def test_groups_restart_together_in_order(world):
    jobs = restarts.plan([result(2, "web"), result(3, "db")])
    assert len(jobs) == 1  # one job for the group, not two
    assert [m["name"] for m in jobs[0]["members"]] == ["db", "web", "cache"]


def test_quiet_hours(world):
    settings.set(world, "general.timezone", "America/New_York")
    tz = ZoneInfo("America/New_York")
    at = lambda h, m=0: datetime(2026, 11, 24, h, m, tzinfo=tz).timestamp()  # noqa: E731
    assert not restarts.in_quiet_hours(world, at(2))  # off by default
    settings.set(world, "quiet.enabled", True)  # 01:00–07:00
    assert restarts.in_quiet_hours(world, at(2)) and not restarts.in_quiet_hours(world, at(7)) and not restarts.in_quiet_hours(world, at(0, 59))
    assert restarts.blocked(world, 1, at(3)) == "quiet_hours"
    settings.set(world, "quiet.start", "22:00"); settings.set(world, "quiet.end", "06:30")  # over midnight
    assert restarts.in_quiet_hours(world, at(23)) and restarts.in_quiet_hours(world, at(6, 15)) and not restarts.in_quiet_hours(world, at(12))


class FakeDocker:
    def __init__(self, names, self_name=None, ollama=None):
        self.names, self.self_name, self.ollama, self.restarted = names, self_name, ollama, []

    async def alist_containers(self):
        return [machines.ContainerInfo(id=n, name=n, image="x", state="running", health=None, exit_code=0, oom_killed=False,
                                       restart_count=0, restart_policy="no", started_at=None, finished_at=None,
                                       log_driver="json-file", compose_project=None, compose_service=None,
                                       is_self=n == self.self_name, is_ollama=n == self.ollama) for n in self.names]

    async def arestart(self, name, timeout=10):
        self.restarted.append(name)


def test_carry_out_restarts_checks_and_reports(world, monkeypatch):
    d = FakeDocker(["db", "web", "cache"], ollama="cache")
    monkeypatch.setattr(machines, "get_machine", lambda mid: d)
    monkeypatch.setattr(restarts, "FOLLOW_UP", (0, 0))
    checked = []

    async def fake_checks(ids, force_ai=False, track=True):
        checked.append(ids)
        return [{"status": "healthy"}]
    job = restarts.plan([result(2, "web")])[0]
    asyncio.run(restarts.carry_out(job, fake_checks))
    assert d.restarted == ["db", "web"]  # never the AI server's container
    assert checked == [[2], [2]]
    events = world.execute("SELECT kind, data FROM events ORDER BY id").fetchall()
    assert [e["kind"] for e in events] == ["restart", "restart", "outgoing"]
    assert "working again" in events[-1]["data"] and "The AI said a restart would help" in events[-1]["data"]


def test_fixing_api(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    f = client.get("/api/fixing").json()
    assert f["mode"] == "watch" and f["max_per_hour"] == 3 and f["cooldown_minutes"] == 5 and f["quiet_enabled"] is False
    assert client.put("/api/fixing", json={"mode": "auto", "quiet_enabled": True, "quiet_start": "23:00"}).json()["mode"] == "auto"
    bad = client.put("/api/fixing", json={"quiet_end": "25:00"})
    assert bad.status_code == 400 and "Quiet hours end" in bad.json()["detail"]
    assert client.put("/api/fixing", json={"mode": "chaos"}).status_code == 400
