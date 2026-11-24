import time

import pytest

from dockllama import ollama

from .fake_ollama import FakeOllama, FakeServer, tag


@pytest.fixture
def fake(monkeypatch):
    f = FakeOllama()
    f.servers["thor"] = FakeServer()
    f.servers["ryzen"] = FakeServer(tags=[tag("gemma4:latest", 6.6, "gemma4", "7.5B", pulled="2026-10-01T00:00:00Z"),
                                          tag("llama3.1:8b", 4.9, "llama", "8.0B", pulled="2026-10-02T00:00:00Z")],
                                    loaded=[])
    monkeypatch.setattr(ollama, "TEST_TRANSPORT", f.transport())
    return f


@pytest.fixture
def c(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    return client


def connect(c, address, **kw):
    return c.post("/api/ai/connect", json={"address": address, **kw}).json()


def test_check_reports_plain_problems(c, fake):
    assert c.post("/api/ai/check", json={"address": "thor"}).json()["ok"] is True
    bad = c.post("/api/ai/check", json={"address": "nobody"}).json()
    assert bad["ok"] is False and bad["problem"]["kind"] == "unreachable"
    assert c.post("/api/ai/check", json={"address": ""}).json()["problem"]["kind"] == "bad_request"


def test_connect_lists_models_and_suggests(c, fake):
    assert connect(c, "thor")["ok"] is True
    st = c.get("/api/ai/status").json()
    assert st["connected"] and st["address"] == "http://thor:11434" and st["model"] is None
    assert c.put("/api/ai/hardware", json={"hardware": "gpu16"}).status_code == 200
    d = c.get("/api/ai/models").json()
    names = [m["name"] for m in d["models"]]
    assert "bge-m3:latest" in names and not next(m for m in d["models"] if m["name"] == "bge-m3:latest")["usable"]
    assert d["suggestion"]["name"] == "gpt-oss:20b"  # loaded already and tested well
    assert d["model"] is None  # suggested, not chosen: the user decides


def test_choosing_a_model(c, fake):
    connect(c, "thor")
    assert c.put("/api/ai/model", json={"name": "bge-m3:latest"}).status_code == 400
    assert c.put("/api/ai/model", json={"name": "not-installed"}).status_code == 404
    assert c.put("/api/ai/model", json={"name": "gemma4:latest"}).json()["model"] == "gemma4:latest"
    assert c.get("/api/ai/status").json()["model"] == "gemma4:latest"


def test_switching_servers_hides_then_restores_data(c, fake):
    connect(c, "thor")
    c.put("/api/ai/hardware", json={"hardware": "gpu16"})
    c.put("/api/ai/model", json={"name": "gpt-oss:20b"})
    r = connect(c, "ryzen")
    assert r["ok"] and r["restored"] is False
    st = c.get("/api/ai/status").json()
    assert st["address"] == "http://ryzen:11434" and st["model"] is None and st["hardware"] == "unknown"
    assert {m["name"] for m in c.get("/api/ai/models").json()["models"]} == {"gemma4:latest", "llama3.1:8b"}
    # Back to Thor: DockLlama recognises it and asks before restoring.
    ask = connect(c, "thor")
    assert ask["ok"] is False and ask["needs_confirm"]["name"] == "thor"
    done = connect(c, "thor", decision="same", server_id=ask["needs_confirm"]["server_id"])
    assert done["ok"] and done["restored"] is True
    st = c.get("/api/ai/status").json()
    assert st["model"] == "gpt-oss:20b" and st["hardware"] == "gpu16"


def test_server_recognised_after_ip_change(c, fake):
    connect(c, "thor")
    c.put("/api/ai/model", json={"name": "llama3.1:8b"})
    connect(c, "ryzen")
    fake.servers["10.0.0.99"] = fake.servers.pop("thor")  # same install, new address
    ask = connect(c, "10.0.0.99")
    assert ask["needs_confirm"]["how"] == "fingerprint" and ask["needs_confirm"]["last_address"] == "http://thor:11434"
    connect(c, "10.0.0.99", decision="same", server_id=ask["needs_confirm"]["server_id"])
    st = c.get("/api/ai/status").json()
    assert st["model"] == "llama3.1:8b" and st["address"] == "http://10.0.0.99:11434"


def test_user_can_say_it_is_a_new_server(c, fake):
    connect(c, "thor")
    connect(c, "ryzen")
    r = connect(c, "thor", decision="new")
    assert r["ok"] and r["restored"] is False
    assert c.get("/api/ai/status").json()["other_servers"] == 2


def test_reconnecting_current_server_does_not_ask(c, fake):
    connect(c, "thor")
    assert connect(c, "thor")["ok"] is True


def test_status_when_server_goes_away(c, fake):
    connect(c, "thor")
    fake.servers.pop("thor")
    st = c.get("/api/ai/status").json()
    assert st["connected"] and st["reachable"] is False and st["problem"]["kind"] == "unreachable"
    d = c.get("/api/ai/models").json()  # falls back to what DockLlama last saw
    assert d["problem"]["kind"] == "unreachable" and len(d["models"]) == 4


def test_download_flow(c, fake):
    connect(c, "thor")
    started = c.post("/api/ai/pull", json={"name": "granite4.2:8b"})
    assert started.status_code == 200
    for _ in range(50):
        p = {x["name"]: x for x in c.get("/api/ai/pulls").json()}["granite4.2:8b"]
        if p["done"]:
            break
        time.sleep(0.02)
    assert p["done"] and p["error"] is None and p["completed"] == p["total"] == 1000
    assert "granite4.2:8b" in [m["name"] for m in c.get("/api/ai/models").json()["models"]]
    c.post("/api/ai/pull", json={"name": "nope:1b"})
    for _ in range(50):
        p = {x["name"]: x for x in c.get("/api/ai/pulls").json()}["nope:1b"]
        if p["done"]:
            break
        time.sleep(0.02)
    assert "no model called" in p["error"]


def test_advanced_endpoints(c, fake):
    connect(c, "thor")
    g = c.get("/api/ai/advanced", params={"name": "llama3.1:8b"}).json()
    assert g["options"]["thinking"] == "none" and g["settings"] == {}
    assert c.put("/api/ai/advanced", params={"name": "llama3.1:8b"}, json={"think": True}).status_code == 400
    r = c.put("/api/ai/advanced", params={"name": "gpt-oss:20b"}, json={"think": "medium", "num_ctx": 16384})
    assert r.status_code == 200 and r.json()["settings"] == {"think": "medium", "num_ctx": 16384}
    assert c.get("/api/ai/advanced", params={"name": "gpt-oss:20b"}).json()["settings"]["num_ctx"] == 16384
    assert next(m for m in c.get("/api/ai/models").json()["models"] if m["name"] == "gpt-oss:20b")["customised"]
    assert c.delete("/api/ai/advanced", params={"name": "gpt-oss:20b"}).json()["settings"] == {}
    assert c.get("/api/ai/advanced", params={"name": "nope"}).status_code == 404


def test_ai_endpoints_need_a_server_and_sign_in(c, fake):
    assert c.get("/api/ai/models").status_code == 409
    assert c.get("/api/ai/status").json()["connected"] is False
    c.cookies.clear()
    assert c.get("/api/ai/status").status_code == 401
