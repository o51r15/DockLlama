import asyncio
import json
import time

import pytest

from dockllama import db, notify


def test_build_urls():
    b = notify.build_url
    assert b("ntfy", {"topic": "dockllama-abc"}) == "ntfys://ntfy.sh/dockllama-abc"
    assert b("ntfy", {"topic": "t1", "server": "http://192.168.1.5:8080"}) == "ntfy://192.168.1.5:8080/t1"
    assert b("ntfy", {"topic": "t1", "server": "ntfy.example.com", "token": "tk_1"}) == "ntfys://ntfy.example.com/t1?token=tk_1"
    assert b("discord", {"webhook": "https://discord.com/api/webhooks/123/abc-DEF"}).startswith("https://discord.com/")
    assert b("telegram", {"bot_token": "123456:ABC-def", "chat_id": "-100123"}) == "tgram://123456:ABC-def/-100123"
    assert b("gotify", {"server": "https://g.example.com", "app_token": "AbC"}) == "gotifys://g.example.com/AbC"
    assert b("pushover", {"user_key": "u1", "app_token": "a1"}) == "pover://u1@a1"
    assert b("email", {"address": "bob@gmail.com", "password": "p w@x"}) == "mailtos://bob:p%20w%40x@gmail.com?to=bob@gmail.com"
    for service, fields, words in [
        ("ntfy", {"topic": ""}, "topic"), ("ntfy", {"topic": "has space"}, "letters"),
        ("discord", {"webhook": "https://example.com/x"}, "Discord webhook"),
        ("telegram", {"bot_token": "nope", "chat_id": "1"}, "BotFather"),
        ("email", {"address": "bob", "password": "x"}, "email address"), ("pigeon", {}, "listed")]:
        with pytest.raises(notify.NotifyError, match=words):
            b(service, fields)
    assert notify.new_ntfy_topic().startswith("dockllama-") and len(notify.new_ntfy_topic()) > 20
    assert notify.qr_svg("https://ntfy.sh/x").startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in notify.qr_svg("x")


@pytest.fixture
def world(conn):
    """Two containers and one ntfy target that wants everything except 'needs a look'."""
    for i, name in enumerate(["web", "db", "a", "b", "c"], 1):
        conn.execute("INSERT INTO containers (id, machine_id, name, monitored, first_seen, last_seen) VALUES (?, 1, ?, 1, 0, 0)",
                     (i, name))
    conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) VALUES ('ntfy', '{}', 'ntfys://ntfy.sh/x', '{}', 0)")
    return conn


def change(conn, cid, to, summary="Something broke.", todo="Fix it.", at=None):
    at = at or time.time()
    prev = conn.execute("SELECT status FROM container_state WHERE container_id = ?", (cid,)).fetchone()
    if prev is None:
        conn.execute("INSERT INTO container_state (container_id, status, since, summary, what_to_do) VALUES (?, ?, ?, ?, ?)",
                     (cid, to, at, summary, todo))
    else:
        conn.execute("UPDATE container_state SET status = ?, since = ?, summary = ?, what_to_do = ? WHERE container_id = ?",
                     (to, at, summary, todo, cid))
    conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'status_change', ?)",
                 (at, cid, json.dumps({"from": prev["status"] if prev else None, "to": to})))


def titles(msgs):
    return [m["title"] for m in msgs]


def test_problem_then_no_repeat_then_recovered(world):
    change(world, 1, "healthy")
    assert notify.plan_messages(world) == []
    change(world, 1, "unhealthy", "It can't reach its database.", "Check the database container.")
    m = notify.plan_messages(world)
    assert titles(m) == ["web: Having problems"] and "What to do: Check the database container." in m[0]["body"]
    change(world, 1, "critical")
    assert notify.plan_messages(world) == []  # still the same problem: no repeat
    assert notify.plan_messages(world) == []
    change(world, 1, "healthy")
    assert titles(notify.plan_messages(world)) == ["web: recovered"]
    change(world, 1, "stopped")
    assert notify.plan_messages(world) == []  # nothing was alerted, nothing to recover


def test_first_round_finds_a_broken_container(world):
    change(world, 2, "critical", "It stopped with code 1.")
    assert titles(notify.plan_messages(world)) == ["db: Down or failing"]


def test_a_burst_becomes_one_message(world):
    for cid in (1, 2, 3, 4, 5):
        change(world, cid, "critical")
    m = notify.plan_messages(world)
    assert titles(m) == ["5 containers need attention"] and m[0]["body"].count("•") == 5


def test_reminders(world):
    now = time.time()
    change(world, 1, "critical", at=now - 7 * 3600)
    notify.plan_messages(world, now=now - 7 * 3600)
    assert notify.plan_messages(world, now=now - 3 * 3600) == []
    m = notify.plan_messages(world, now=now)
    assert titles(m) == ["Still broken: web"] and "for 7 hours" in m[0]["body"]
    assert notify.plan_messages(world, now=now + 60) == []  # next one in another 6 hours
    world.execute("UPDATE settings SET value = '0' WHERE key = 'notify.reminder_hours'")
    from dockllama import settings
    settings.set(world, "notify.reminder_hours", 0)
    assert notify.plan_messages(world, now=now + 30 * 3600) == []


def test_needs_a_look_is_opt_in(world):
    change(world, 1, "degraded")
    assert notify.plan_messages(world) == []
    change(world, 1, "healthy")
    notify.plan_messages(world)
    world.execute("UPDATE notify_targets SET events = ?", (json.dumps({"needs_look": True}),))
    change(world, 1, "degraded", "Slow responses.")
    m = notify.plan_messages(world)
    assert titles(m) == ["web: Needs a look"] and m[0]["kind"] == "needs_look"


def test_cant_check_alert_once_and_all_clear(world):
    for cid in (1, 2):
        change(world, cid, "unknown", "Couldn't check: Nothing answered at http://thor:11434.")
        check = world.execute("INSERT INTO checks (container_id, at, status, source) VALUES (?, 0, 'unknown', 'ai')",
                              (cid,)).lastrowid
        world.execute("UPDATE container_state SET unknown_streak = 2, last_check_id = ? WHERE container_id = ?", (check, cid))
    assert [m for m in notify.plan_messages(world) if m["kind"] == "checking"] == []
    world.execute("UPDATE container_state SET unknown_streak = 3")
    m = [m for m in notify.plan_messages(world) if m["kind"] == "checking"]
    assert titles(m) == ["DockLlama can't reach its AI server"] and "web, db" in m[0]["body"]
    assert [m for m in notify.plan_messages(world) if m["kind"] == "checking"] == []
    change(world, 1, "healthy"); change(world, 2, "healthy")
    assert "DockLlama is checking normally again" in titles(notify.plan_messages(world))


def test_links_use_the_saved_address(world):
    from dockllama import settings
    settings.set(world, "general.base_url", "http://192.168.1.10:8556")
    change(world, 1, "critical")
    assert notify.plan_messages(world)[0]["body"].endswith("http://192.168.1.10:8556/container?id=1")


def test_deliver_and_record(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_now", lambda url, title, body: (sent.append((url, title)) or (True, "")))
    conn.execute("INSERT INTO containers (id, machine_id, name, monitored, first_seen, last_seen) VALUES (1, 1, 'web', 1, 0, 0)")
    conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) VALUES ('ntfy', '{}', 'u1', '{}', 0)")
    conn.execute("INSERT INTO notify_targets (service, fields, url, events, created_at) VALUES "
                 "('discord', '{}', 'u2', '{\"recoveries\": false}', 0)")
    change(conn, 1, "critical")
    assert asyncio.run(notify.process()) == 2
    change(conn, 1, "healthy")
    asyncio.run(notify.process())
    assert sent == [("u1", "web: Down or failing"), ("u2", "web: Down or failing"), ("u1", "web: recovered")]
    with db.session() as c2:
        log = c2.execute("SELECT COUNT(*) n FROM events WHERE kind = 'notify'").fetchone()["n"]
        assert log == 3 and c2.execute("SELECT last_ok FROM notify_targets WHERE id = 1").fetchone()["last_ok"]


def test_api(client, monkeypatch):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    sent = []
    monkeypatch.setattr(notify, "send_now", lambda url, title, body: (sent.append(url) or (True, "")))
    topic = client.get("/api/notify/ntfy-topic").json()["topic"]
    r = client.post("/api/notify/test", json={"service": "ntfy", "fields": {"topic": topic}})
    assert r.json()["ok"] and sent == [f"ntfys://ntfy.sh/{topic}"]
    bad = client.post("/api/notify/targets", json={"service": "telegram", "fields": {"bot_token": "x", "chat_id": "1"}})
    assert bad.status_code == 400 and "BotFather" in bad.json()["detail"]
    t = client.post("/api/notify/targets", json={"service": "ntfy", "fields": {"topic": topic},
                                                 "base_url": "http://10.0.0.5:8556"}).json()
    assert t["subscribe"] == f"https://ntfy.sh/{topic}" and t["events"]["problems"] and not t["events"]["needs_look"]
    assert client.get("/api/notify/qr", params={"text": t["subscribe"]}).text.startswith("<svg")
    lst = client.get("/api/notify/targets").json()
    assert lst["base_url"] == "http://10.0.0.5:8556" and "url" not in lst["targets"][0] and topic in lst["targets"][0]["description"]
    assert client.patch(f"/api/notify/targets/{t['id']}", json={"events": {"recoveries": False}}).json()["events"]["recoveries"] is False
    assert client.post(f"/api/notify/targets/{t['id']}/test").json()["ok"]
    assert client.put("/api/notify/settings", json={"reminder_hours": 500}).status_code == 400
    client.delete(f"/api/notify/targets/{t['id']}")
    assert client.get("/api/notify/targets").json()["targets"] == []


def test_notify_form_copies_match_the_partial():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "frontend"
    part = (root / "partials" / "notify-form.html").read_text()
    for page in ("settings.html", "welcome.html"):
        assert part in (root / page).read_text(), f"{page} has an out-of-date copy of partials/notify-form.html"
