import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dockllama import ai_servers, db, ollama, settings, summary

from .fake_ollama import FakeOllama, FakeServer, tag


def seed(conn, now):
    for i, name in enumerate(["web", "db", "plex", "job"], 1):
        conn.execute("INSERT INTO containers (id, machine_id, name, monitored, first_seen, last_seen) VALUES (?, 1, ?, 1, 0, 0)",
                     (i, name))
    rows = [(1, "healthy", "Fine."), (2, "critical", "It stopped with code 1."), (3, "healthy", "Fine."), (4, "stopped", "Done.")]
    for cid, st, s in rows:
        conn.execute("INSERT INTO container_state (container_id, status, since, summary) VALUES (?, ?, ?, ?)", (cid, st, now - 7200, s))
    conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, 3, 'status_change', ?)",
                 (now - 5000, json.dumps({"from": "unhealthy", "to": "healthy"})))
    conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, 3, 'restart', ?)",
                 (now - 5100, json.dumps({"by": "dockllama"})))
    conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, 1, 'restart', ?)",
                 (now - 90000, json.dumps({"by": "you"})))  # older than a day


def test_template(conn):
    now = time.time()
    seed(conn, now)
    data = summary.gather(conn, now)
    assert data["fine"] == 2 and data["stopped"] == 1 and len(data["restarts_last_24h"]) == 1
    text = summary.template(data)
    assert text.startswith("1 needs attention, 1 restart, 2 fine, 1 stopped.")
    assert "• db: Down or failing. It stopped with code 1." in text and "Recovered in the last day: plex." in text
    conn.execute("DELETE FROM container_state WHERE container_id = 2")
    assert "Nothing needed you" not in summary.template(summary.gather(conn, now))  # plex recovered


def test_quiet_day(conn):
    now = time.time()
    conn.execute("INSERT INTO containers (id, machine_id, name, monitored, first_seen, last_seen) VALUES (1, 1, 'web', 1, 0, 0)")
    conn.execute("INSERT INTO container_state (container_id, status, since) VALUES (1, 'healthy', 0)")
    assert summary.template(summary.gather(conn, now)) == "1 fine.\nNothing needed you in the last 24 hours."


def test_due_once_a_day_at_local_time(conn):
    settings.set(conn, "general.timezone", "America/New_York")
    settings.set(conn, "summary.time", "08:00")
    tz = ZoneInfo("America/New_York")
    at = lambda h: datetime(2026, 11, 24, h, 5, tzinfo=tz).timestamp()  # noqa: E731
    assert not summary.due(conn, at(7)) and summary.due(conn, at(8))
    settings.set(conn, "summary.last_sent", "2026-11-24")
    assert not summary.due(conn, at(9))
    settings.set(conn, "summary.enabled", False)
    settings.set(conn, "summary.last_sent", None)
    assert not summary.due(conn, at(9))


def test_ai_writes_it_and_falls_back(conn, monkeypatch):
    now = time.time()
    seed(conn, now)
    f = FakeOllama()
    f.servers["thor"] = FakeServer(tags=[tag("llama3.1:8b", 4.9, "llama", "8.0B")], loaded=[])
    monkeypatch.setattr(ollama, "TEST_TRANSPORT", f.transport())
    sid = ai_servers.connect(conn, "http://thor:11434", "0.40.17", [], None)
    ai_servers.save_models(conn, sid, asyncio.run(ollama.OllamaClient("http://thor:11434").models()), [])
    conn.execute("UPDATE ai_servers SET model = 'llama3.1:8b'")
    f.servers["thor"].generate_response = {"response": "db is down since 2 hours ago. Plex had a wobble but is fine.",
                                           "done": True, "done_reason": "stop"}
    title, body, by_ai = asyncio.run(summary.build(now))
    assert by_ai and body.startswith("db is down") and title == "DockLlama daily summary: 1 needs attention"
    sent = f.servers["thor"].generate_calls[-1]
    assert '"problems_now"' in sent["prompt"] and "format" not in sent and "INFO" not in sent["prompt"]
    f.servers["thor"].generate_response = {"response": "db is", "done": True, "done_reason": "length"}
    _, body, by_ai = asyncio.run(summary.build(now))
    assert not by_ai and body.startswith("1 needs attention")


def test_send_queues_through_notifications(conn):
    seed(conn, time.time())
    r = asyncio.run(summary.send())
    with db.session() as c2:
        out = c2.execute("SELECT data FROM events WHERE kind = 'outgoing'").fetchone()
        assert json.loads(out["data"])["kind"] == "summary" and settings.get(c2, "summary.last_sent")
    assert r["by_ai"] is False


def test_api(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    assert client.get("/api/summary").json()["time"] == "08:00"
    assert client.put("/api/summary", json={"time": "18:30", "enabled": True}).json()["time"] == "18:30"
    assert client.put("/api/summary", json={"time": "6pm"}).status_code == 400
    p = client.post("/api/summary/preview").json()
    assert p["title"].startswith("DockLlama daily summary") and p["by_ai"] is False


def test_only_watched_containers_count(conn):
    now = time.time()
    seed(conn, now)
    conn.execute("UPDATE containers SET monitored = 0 WHERE name = 'plex'")
    data = summary.gather(conn, now)
    assert data["restarts_last_24h"] == [] and data["changes_last_24h"] == []
