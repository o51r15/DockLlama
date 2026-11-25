"""How often to check: never shorter than a check round takes here."""

import time

from dockllama import db, settings


def _signed_in(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})


def _watch(n):
    with db.session() as conn, db.transaction(conn):
        now = time.time()
        for i in range(n):
            conn.execute("INSERT INTO containers (machine_id, name, monitored, first_seen, last_seen) VALUES (1, ?, 1, ?, ?)",
                         (f"c{i}", now, now))


def test_estimate_before_any_round_then_the_measured_time(client):
    _signed_in(client)
    _watch(100)  # without AI each check is ~0.2 s: a round of ~20 s fits every minute
    f = client.get("/api/fixing").json()
    assert f["round_measured"] is False and f["min_interval"] == 1
    with db.session() as conn:
        settings.set(conn, "checks.last_round_s", 30 * 100)  # measured: 100 containers × 30 s = 50 minutes
    f = client.get("/api/fixing").json()
    assert f["round_measured"] is True and f["round_seconds"] == 3000 and f["min_interval"] == 63  # 50 min + 25%
    r = client.put("/api/fixing", json={"interval_minutes": 10})
    assert r.status_code == 400 and "50 minutes" in r.json()["detail"] and "63 minutes" in r.json()["detail"]
    assert client.put("/api/fixing", json={"interval_minutes": 90}).status_code == 200


def test_short_rounds_allow_short_intervals(client):
    _signed_in(client)
    with db.session() as conn:
        settings.set(conn, "checks.last_round_s", 40)
    f = client.get("/api/fixing").json()
    assert f["min_interval"] == 1
    assert client.put("/api/fixing", json={"interval_minutes": 2}).status_code == 200


def test_a_round_records_how_long_it_took(data_dir, monkeypatch):
    import asyncio

    from dockllama import checks

    from .test_checks import FakeDocker, first_round, setup_db  # noqa: F401
    d = FakeDocker()
    monkeypatch.setattr(checks.machines, "get_machine", lambda mid: d)
    setup_db([], with_ai=False)
    d.add("web")
    first_round(d)
    with db.session() as conn:
        took = settings.get(conn, "checks.last_round_s")
    assert took is not None and took >= 0
    # a single Check now doesn't count as a round
    with db.session() as conn:
        settings.set(conn, "checks.last_round_s", 999)
        cid = conn.execute("SELECT id FROM containers WHERE name = 'web'").fetchone()["id"]
    asyncio.run(checks.run_checks([cid], track=False))
    with db.session() as conn:
        assert settings.get(conn, "checks.last_round_s") == 999


def test_coverage_counts_and_choosing_containers(client, monkeypatch):
    """Auto-restart is turned on together with the containers it covers; the counts say why
    the rest aren't covered (not chosen, or on a machine that only allows watching)."""
    from dockllama import machines
    _signed_in(client)
    _watch(3)
    with db.session() as conn, db.transaction(conn):
        rid = conn.execute("INSERT INTO machines (name, kind, url, token, allows_restart, enabled) "
                           "VALUES ('optiplex', 'remote', 'http://192.168.1.192:8557', 't', 0, 1)").lastrowid
        now = time.time()
        conn.execute("INSERT INTO containers (machine_id, name, monitored, first_seen, last_seen) VALUES (?, 'radarr', 1, ?, ?)",
                     (rid, now, now))
        conn.execute("INSERT INTO containers (machine_id, name, monitored, first_seen, last_seen) VALUES (1, 'unwatched', 0, ?, ?)",
                     (now, now))

    class Info:
        def __init__(self, name, is_self=False, is_ollama=False):
            self.name, self.is_self, self.is_ollama = name, is_self, is_ollama

    class Machine:
        def __init__(self, infos):
            self.infos = infos

        async def alist_containers(self):
            return self.infos
    fakes = {1: Machine([Info("c0"), Info("c1", is_ollama=True), Info("c2")]), rid: Machine([Info("radarr")])}
    monkeypatch.setattr(machines, "get_machine", lambda mid: fakes[mid])

    f = client.get("/api/fixing").json()
    assert f["mode"] == "watch" and f["coverage"] == {"watched": 4, "covered": 0, "not_allowed": 3, "machine_watch_only": 1}
    cov = client.get("/api/fixing/coverage").json()
    local, remote = cov["machines"]
    assert local["kind"] == "local" and [c["name"] for c in local["containers"]] == ["c0", "c1", "c2"]
    assert next(c for c in local["containers"] if c["name"] == "c1")["never"] == "runs your AI"
    assert remote["name"] == "optiplex" and remote["allows_restart"] is False

    ids = {c["name"]: c["id"] for c in local["containers"]}
    f = client.put("/api/fixing/coverage", json={"allowed_ids": [ids["c0"], ids["c2"]], "turn_on": True}).json()
    assert f["mode"] == "auto" and f["coverage"]["covered"] == 2 and f["coverage"]["not_allowed"] == 1
    # editing later replaces the choice; containers left out are turned off
    f = client.put("/api/fixing/coverage", json={"allowed_ids": [ids["c2"]]}).json()
    assert f["coverage"]["covered"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT allow_auto_restart FROM containers WHERE name = 'c0'").fetchone()[0] == 0
        assert conn.execute("SELECT allow_auto_restart FROM containers WHERE name = 'unwatched'").fetchone()[0] == 0
