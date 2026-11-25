"""Web page checks: local addresses only, what counts as answering, and the 3-in-a-row rule."""

import asyncio

import httpx
import pytest

from dockllama import db, rules, webcheck
from dockllama.restarts import wants_restart

from .test_checks import FakeDocker, docker, fake, first_round, run, setup_db, state, verdict


@pytest.mark.parametrize("url", [
    "http://192.168.1.20:8080", "http://10.0.0.5", "https://172.20.0.3:8443/", "http://127.0.0.1:9000",
    "http://host.docker.internal:8096", "http://jellyfin:8096", "http://nas.lan:5000", "http://box.local",
    "http://media.home.arpa", "http://[fd00::5]:80", "http://100.101.1.90:8096",
])
def test_local_addresses_are_allowed(url):
    assert webcheck.address_problem(url) is None


@pytest.mark.parametrize("url", ["https://jellyfin.example.com", "http://8.8.8.8", "http://mysite.org:8080",
                                 "https://203.0.113.9"])
def test_public_addresses_are_refused_with_the_reason(url):
    assert "Cloudflare" in webcheck.address_problem(url)


def test_bad_addresses():
    assert "http://" in webcheck.address_problem("192.168.1.5:8080")
    assert "http://" in webcheck.address_problem("ftp://192.168.1.5")


def test_suggestions_from_published_ports():
    inspect = {"NetworkSettings": {"Ports": {
        "8096/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8096"}, {"HostIp": "::", "HostPort": "8096"}],
        "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8443"}],
        "1900/udp": [{"HostIp": "0.0.0.0", "HostPort": "1900"}],
        "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000"}],  # only reachable from the host
        "5432/tcp": None}}}
    assert webcheck.suggestions(inspect, None) == ["https://host.docker.internal:8443", "http://host.docker.internal:8096"]
    assert webcheck.suggestions(inspect, "http://192.168.1.192:8557") == ["https://192.168.1.192:8443",
                                                                          "http://192.168.1.192:8096"]


def _serve(monkeypatch, handler):
    monkeypatch.setattr(webcheck, "TEST_TRANSPORT", httpx.MockTransport(handler))


def check(url="http://192.168.1.20:8080", word=""):
    return asyncio.run(webcheck.check(url, word))


def test_what_counts_as_answering(monkeypatch):
    for code, ok in ((200, True), (302, True), (401, True), (404, True), (500, False), (502, False)):
        _serve(monkeypatch, lambda r, code=code: httpx.Response(code, headers={"location": "/login"}))
        assert check()[0] is ok, code

    def refused(r):
        raise httpx.ConnectError("[Errno 111] Connection refused")
    _serve(monkeypatch, refused)
    assert check() == (False, "Nothing is answering at that address (connection refused).")

    def slow(r):
        raise httpx.ReadTimeout("timed out")
    _serve(monkeypatch, slow)
    assert check() == (False, "No answer within 10 seconds.")


def test_required_word_follows_local_redirects_only(monkeypatch):
    seen = []

    def app(r):
        seen.append(str(r.url))
        if r.url.path == "/":
            return httpx.Response(302, headers={"location": "/web/index.html"})
        if r.url.path == "/web/index.html":
            return httpx.Response(200, text="<title>Jellyfin</title>")
        return httpx.Response(404)
    _serve(monkeypatch, app)
    assert check(word="jellyfin")[0] is True
    assert check(word="plex") == (False, 'The page answered but didn\'t contain "plex".')

    def off_site(r):
        seen.append(str(r.url))
        return httpx.Response(302, headers={"location": "https://auth.example.com/login"})
    seen.clear()
    _serve(monkeypatch, off_site)
    assert check(word="jellyfin")[0] is False
    assert seen == ["http://192.168.1.20:8080"]  # never follows a redirect off the local address


def test_public_address_is_never_contacted(monkeypatch):
    def boom(r):
        raise AssertionError("contacted " + str(r.url))
    _serve(monkeypatch, boom)
    assert check("https://jellyfin.example.com")[0] is False


def test_rule_only_after_three_in_a_row():
    f = rules.Facts(name="web", state="running", web_fails=2, web_error="No answer within 10 seconds.")
    assert rules.evaluate(f, None).status is None
    f.web_fails = 3
    r = rules.evaluate(f, None)
    assert r.status == "unhealthy" and r.rule == "web_failing" and "may be frozen" in r.reason
    assert "No answer" in r.reason
    assert wants_restart({"docker_state": "running", "rule": "web_failing", "status": "unhealthy"}, {})


def test_round_marks_a_frozen_container_and_recovers(data_dir, docker, fake, monkeypatch):
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict()
    docker.add("web")
    docker.log("web", "INFO started")
    first_round(docker)
    with db.session() as conn, db.transaction(conn):
        conn.execute("UPDATE containers SET web_url = 'http://192.168.1.20:8080' WHERE name = 'web'")
    answering = {"on": False}

    def app(r):
        if not answering["on"]:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200)
    _serve(monkeypatch, app)
    calls = len(fake.servers["thor"].generate_calls)
    for _ in (1, 2):
        run()
        assert state("web")["status"] == "healthy"
    with db.session() as conn:
        assert conn.execute("SELECT web_fails FROM containers WHERE name = 'web'").fetchone()[0] == 2
    run()  # third failure in a row
    st = state("web")
    assert st["status"] == "unhealthy" and "web page stopped answering" in st["summary"]
    asked = len(fake.servers["thor"].generate_calls) - calls
    assert asked == 1  # the AI is asked once when it goes down, not every failed round
    run()
    assert len(fake.servers["thor"].generate_calls) - calls == 1
    answering["on"] = True
    run()
    st = state("web")
    assert st["status"] == "healthy"
    with db.session() as conn:
        row = conn.execute("SELECT web_fails, web_last_ok, web_last_error FROM containers WHERE name = 'web'").fetchone()
    assert row["web_fails"] == 0 and row["web_last_ok"] and row["web_last_error"] is None


def test_api_refuses_public_address_and_tries_one(client, monkeypatch, docker):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    docker.add("web")
    asyncio.run(__import__("dockllama.checks", fromlist=["x"]).run_checks())
    with db.session() as conn:
        cid = conn.execute("SELECT id FROM containers WHERE name = 'web'").fetchone()["id"]
    r = client.patch(f"/api/containers/{cid}", json={"web_url": "https://web.example.com"})
    assert r.status_code == 400 and "Cloudflare" in r.json()["detail"]
    assert client.patch(f"/api/containers/{cid}", json={"web_url": "http://host.docker.internal:8080"}).status_code == 200
    _serve(monkeypatch, lambda r: httpx.Response(200))
    assert client.post(f"/api/containers/{cid}/web-check/test", json={"url": "http://10.0.0.2"}).json()["ok"] is True
    info = client.get(f"/api/containers/{cid}/web-check").json()
    assert info["web_url"] == "http://host.docker.internal:8080" and info["fails_needed"] == 3
    assert client.patch(f"/api/containers/{cid}", json={"web_url": ""}).json()["web_url"] is None
