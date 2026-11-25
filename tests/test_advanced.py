"""Settings → Advanced: retention, log lines per check, forgetting old AI servers."""

import time

from dockllama import checks, db, settings

from .test_model_tests import c, fake  # noqa: F401  (fixtures)


def _container(conn) -> int:
    m = conn.execute("SELECT id FROM machines LIMIT 1").fetchone()
    return conn.execute("INSERT INTO containers (machine_id, name, monitored, first_seen, last_seen) VALUES (?, 'web', 1, 0, 0)",
                        (m["id"],)).lastrowid


def test_defaults_and_limits(c):
    a = c.get("/api/advanced").json()
    assert a["retention_days"] == 30 and a["log_lines"] == 300
    assert c.put("/api/advanced", json={"retention_days": 0}).status_code == 400
    assert c.put("/api/advanced", json={"log_lines": 5}).status_code == 400
    a = c.put("/api/advanced", json={"retention_days": 365, "log_lines": 500}).json()
    assert a["retention_days"] == 365 and a["log_lines"] == 500


def test_prune_follows_the_setting_and_keeps_what_matters(c):
    now = time.time()
    with db.session() as conn, db.transaction(conn):
        cid = _container(conn)
        old, new, current = (conn.execute(
            "INSERT INTO checks (container_id, at, status, source) VALUES (?, ?, 'healthy', 'ai')", (cid, t)).lastrowid
            for t in (now - 40 * 86400, now - 10 * 86400, now - 50 * 86400))
        conn.execute("INSERT INTO container_state (container_id, status, since, last_check_id) VALUES (?, 'healthy', 0, ?)",
                     (cid, current))
        conn.execute("INSERT INTO stats (container_id, at, cpu, mem) VALUES (?, ?, 1, 1)", (cid, now - 40 * 86400))
        conn.execute("INSERT INTO stats (container_id, at, cpu, mem) VALUES (?, ?, 1, 1)", (cid, now - 5 * 86400))
        for kind, handled in (("status_change", 1), ("status_change", 0), ("restart", 0)):
            conn.execute("INSERT INTO events (at, container_id, kind, handled) VALUES (?, ?, ?, ?)",
                         (now - 40 * 86400, cid, kind, handled))
        gone = checks.prune(conn)
    assert gone == {"checks": 1, "stats": 1, "events": 2}
    with db.session() as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM checks")}
        assert ids == {new, current}  # the current status's check survives however old it is
        left = [(r["kind"], r["handled"]) for r in conn.execute("SELECT kind, handled FROM events")]
        assert left == [("status_change", 0)]  # a message not sent yet is never deleted
    # shortening the period takes effect straight away
    c.put("/api/advanced", json={"retention_days": 7})
    with db.session() as conn:
        assert {r["id"] for r in conn.execute("SELECT id FROM checks")} == {current}
        assert conn.execute("SELECT COUNT(*) n FROM stats").fetchone()["n"] == 1


def test_log_lines_setting_is_what_checks_use(c):
    c.put("/api/advanced", json={"log_lines": 1000})
    with db.session() as conn:
        assert settings.get(conn, "checks.log_lines") == 1000


def test_forget_old_server_but_never_the_connected_one(c, fake):
    c.get("/api/ai/models")
    with db.session() as conn, db.transaction(conn):
        connected = conn.execute("SELECT id FROM ai_servers WHERE connected = 1").fetchone()["id"]
        old = conn.execute("INSERT INTO ai_servers (name, address, connected, created_at, last_seen) "
                           "VALUES ('old box', 'http://10.0.0.9:11434', 0, 0, 1)").lastrowid
        conn.execute("INSERT INTO models (server_id, name, first_seen) VALUES (?, 'x:1', 0)", (old,))
    servers = c.get("/api/advanced/servers").json()["servers"]
    assert [s["name"] for s in servers] == ["old box"] and servers[0]["models"] == 1
    assert c.delete(f"/api/advanced/servers/{connected}").status_code == 409
    assert c.delete(f"/api/advanced/servers/{old}").json() == {"ok": True}
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM models WHERE server_id = ?", (old,)).fetchone()["n"] == 0
    assert c.get("/api/advanced/servers").json()["servers"] == []
