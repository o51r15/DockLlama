import asyncio
import json
import time

import httpx
import pytest

from dockllama import ai_servers, db, evaluate, model_tests, ollama
from dockllama.model_tests import Progress, TooSlow, meaningfully_better, test_model as run_model_test
from dockllama.scenarios import SCENARIOS, score_answer

from .fake_ollama import FakeOllama, FakeServer, perfect_answer, tag

# --- reading answers --------------------------------------------------------------------


def test_parse_answer_is_lenient_where_harmless():
    good = {"status": "Healthy", "health_score": "92", "confidence": 80, "summary": "All fine here, nothing to do.",
            "what_to_do": "Nothing.", "restart_would_help": "false", "recommended_action": "none"}
    v = evaluate.parse_answer("<think>hmm</think>\n```json\n" + json.dumps(good) + "\n```")
    assert v["status"] == "healthy" and v["health_score"] == 92 and v["restart_would_help"] is False
    assert evaluate.parse_answer(json.dumps(dict(good, recommended_action="reboot")))["recommended_action"] == "none"
    for bad in ["not json", "[1, 2]", json.dumps(dict(good, status="fine")),
                json.dumps({k: v for k, v in good.items() if k != "summary"}),
                json.dumps(dict(good, health_score=9.5)), json.dumps(dict(good, restart_would_help="maybe"))]:
        with pytest.raises(evaluate.AnswerError):
            evaluate.parse_answer(bad)


def test_rubric_fixes_and_full_marks():
    s2 = next(s for s in SCENARIOS if s["id"] == "S2")
    assert s2["expect_status"] == "critical" and s2["expect_restart"] is False and s2["expect_action"] == "none"
    assert next(s for s in SCENARIOS if s["id"] == "S8")["expect_status"] == "degraded"
    for sc in SCENARIOS:
        verdict = evaluate.parse_answer(perfect_answer({"prompt": sc["summary"]})["response"])
        assert score_answer(sc, verdict) == (100, [])
    # partial credit, as in v1: healthy expected, degraded given, score just outside the range
    s1 = SCENARIOS[0]
    v = {"status": "degraded", "health_score": 78, "recommended_action": "none", "restart_would_help": False,
         "summary": "Probably fine but slightly worried."}
    assert score_answer(s1, v)[0] == 12 + 15 + 20 + 10 + 10


def test_schema_is_sent_and_prompt_has_the_rubric_fixes():
    assert "restarting it does NOT help" in evaluate.SYSTEM_PROMPT and "**degraded**, error_origin" in evaluate.SYSTEM_PROMPT
    assert set(evaluate.SCHEMA["properties"]["status"]["enum"]) == set(evaluate.STATUSES)


# --- testing one model --------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch):
    f = FakeOllama()
    f.servers["thor"] = FakeServer(loaded=[])
    f.servers["thor"].generate_handler = perfect_answer
    monkeypatch.setattr(ollama, "TEST_TRANSPORT", f.transport())
    return f


def _model(name="llama3.1:8b", thinking="none", levels=()):
    return ollama.ModelDetails(name=name, digest="abc", modified_at="t", size=5 * 10**9, family="llama",
                               parameter_size="8B", capabilities=["completion"], max_context=131072,
                               thinking=thinking, think_levels=list(levels))


def _run(coro):
    return asyncio.run(coro)


def test_a_perfect_model_scores_100_and_is_unloaded(fake):
    client = ollama.OllamaClient("http://thor:11434")
    p = Progress(1, "llama3.1:8b")
    r = _run(run_model_test(client, _model(), {}, p, keep_loaded=False))
    assert r["status"] == "done" and r["score"] == 100.0 and r["fit"] == "gpu" and r["cut_off"] == 0
    assert r["sec_per_check"] == 1.5 and r["load_s"] == 0.5
    calls = fake.servers["thor"].generate_calls
    assert len(calls) == 8 and all(c["keep_alive"] == "10m" and c["format"]["type"] == "object" for c in calls)
    assert all(c["system"] == evaluate.SYSTEM_PROMPT and c["options"]["num_ctx"] == 8192 for c in calls)
    assert "think" not in calls[0]
    assert fake.servers["thor"].unloaded == ["llama3.1:8b"]


def test_models_in_use_elsewhere_stay_loaded_and_partial_fit_is_seen(fake):
    fake.servers["thor"].vram_fraction = 0.6
    r = _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {}, Progress(1, "x"), keep_loaded=True))
    assert r["fit"] == "partial" and fake.servers["thor"].unloaded == []


def test_thinking_models_are_tested_with_their_settings(fake):
    gpt = _model("gpt-oss:20b", "levels", ["low", "medium", "high"])
    _run(run_model_test(ollama.OllamaClient("http://thor:11434"), gpt, {"think": "high", "num_ctx": 16384},
                        Progress(1, "x"), keep_loaded=False))
    call = fake.servers["thor"].generate_calls[0]
    assert call["think"] == "high" and call["options"]["num_ctx"] == 16384 and call["options"]["num_predict"] == 2000


def test_bad_json_gets_one_more_try_at_90_percent(fake):
    seen = {}

    def flaky(payload, request):
        key = payload["prompt"][:40]
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1 and "IMPORTANT" not in payload["prompt"]:
            return {"response": "Sure! The container is fine.", "done": True, "done_reason": "stop"}
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = flaky
    r = _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {}, Progress(1, "x"), keep_loaded=False))
    assert r["score"] == 90.0 and all(d["retried"] for d in r["details"])
    retry = fake.servers["thor"].generate_calls[1]
    assert retry["prompt"].endswith(evaluate.RETRY_SUFFIX.strip()) and retry["options"]["temperature"] == 0.0


def test_unreadable_twice_scores_zero_for_that_question(fake):
    def bad_s3(payload, request):
        if "test-api-gateway" in payload["prompt"]:
            return {"response": "{", "done": True, "done_reason": "length"}
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = bad_s3
    r = _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {}, Progress(1, "x"), keep_loaded=False))
    assert r["score"] == 87.5 and r["cut_off"] == 1
    s3 = next(d for d in r["details"] if d["id"] == "S3")
    assert s3["score"] == 0 and "Couldn't read" in s3["notes"][0]


def test_first_question_timing_out_stops_early_as_too_slow(fake):
    def slow(payload, request):
        raise httpx.ReadTimeout("slow", request=request)
    fake.servers["thor"].generate_handler = slow
    with pytest.raises(TooSlow):
        _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {"timeout": 5}, Progress(1, "x"),
                            keep_loaded=False))
    assert len(fake.servers["thor"].generate_calls) == 1  # didn't spend 8 × timeout on it
    assert fake.servers["thor"].unloaded == ["llama3.1:8b"]


def test_crawling_model_stops_early(fake):
    def crawl(payload, request):
        return dict(perfect_answer(payload), eval_count=10, eval_duration=10_000_000_000)  # 1 token/s
    fake.servers["thor"].generate_handler = crawl
    with pytest.raises(TooSlow, match="1.0 tokens a second"):
        _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {}, Progress(1, "x"), keep_loaded=False))


def test_a_late_timeout_is_retried_once_at_the_end(fake):
    hits = {"n": 0}

    def busy_once(payload, request):
        if "test-plex-transcoder" in payload["prompt"]:
            hits["n"] += 1
            if hits["n"] == 1:
                raise httpx.ReadTimeout("busy", request=request)
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = busy_once
    r = _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {}, Progress(1, "x"), keep_loaded=False))
    assert r["score"] == 100.0 and hits["n"] == 2
    assert [d["id"] for d in r["details"]] == [s["id"] for s in SCENARIOS]


def test_meaningfully_better():
    cur = {"status": "done", "score": 94.0, "sec_per_check": 2.0, "fit": "gpu"}
    assert meaningfully_better({"status": "done", "score": 97.0, "sec_per_check": 2.4, "fit": "gpu"}, cur)
    assert not meaningfully_better({"status": "done", "score": 96.0, "sec_per_check": 2.0, "fit": "gpu"}, cur)
    assert not meaningfully_better({"status": "done", "score": 99.0, "sec_per_check": 9.0, "fit": "gpu"}, cur)
    assert meaningfully_better({"status": "done", "score": 93.0, "sec_per_check": 1.2, "fit": "gpu"}, cur)
    assert not meaningfully_better({"status": "done", "score": 99.0, "sec_per_check": 2.0, "fit": "partial"}, cur)
    assert meaningfully_better({"status": "done", "score": 80.0, "sec_per_check": 2.0, "fit": "gpu"},
                               {"status": "too_slow", "score": None})
    assert not meaningfully_better({"status": "done", "score": 60.0, "sec_per_check": 2.0, "fit": "gpu"}, None)
    assert not meaningfully_better({"status": "too_slow", "score": None}, None)


def test_tonight_is_the_next_quiet_hours_start(conn):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from dockllama import settings
    settings.set(conn, "general.timezone", "America/New_York")
    tz = ZoneInfo("America/New_York")
    evening = datetime(2026, 11, 23, 22, 0, tzinfo=tz).timestamp()
    assert datetime.fromtimestamp(model_tests.tonight(conn, evening), tz) == datetime(2026, 11, 24, 1, 0, tzinfo=tz)
    early = datetime(2026, 11, 24, 0, 30, tzinfo=tz).timestamp()
    assert datetime.fromtimestamp(model_tests.tonight(conn, early), tz) == datetime(2026, 11, 24, 1, 0, tzinfo=tz)


# --- the runner and the API ---------------------------------------------------------------


@pytest.fixture
def c(client, fake):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    fake.servers["thor"].tags = [tag("gemma4:latest", 6.6, "gemma4", "7.5B"), tag("llama3.1:8b", 4.9, "llama", "8.0B"),
                                 tag("bge-m3:latest", 1.2, "bert", "566.70M")]
    assert client.post("/api/ai/connect", json={"address": "thor"}).json()["ok"]
    client.put("/api/ai/hardware", json={"hardware": "gpu8"})
    client.put("/api/ai/model", json={"name": "llama3.1:8b"})
    return client


def wait_idle(c, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        st = c.get("/api/ai/tests").json()
        if not st["busy"] and st["active"] is None:
            return st
        time.sleep(0.05)
    raise AssertionError(f"tests still running: {st}")


def test_test_one_model_through_the_api(c, fake):
    r = c.post("/api/ai/tests", json={"names": ["gemma4:latest"]})
    assert r.status_code == 200
    st = wait_idle(c)
    res = st["results"]["gemma4:latest"]
    assert res["status"] == "done" and res["score"] == 100.0 and not res["older_version"]
    hist = c.get("/api/ai/tests/history", params={"name": "gemma4:latest"}).json()["results"]
    assert len(hist) == 1 and len(hist[0]["details"]) == 8
    models = c.get("/api/ai/models").json()
    assert next(m for m in models["models"] if m["name"] == "gemma4:latest")["result"]["score"] == 100.0
    # tested results drive the suggestion now
    assert models["suggestion"]["name"] == "gemma4:latest" and "Best result on this server" in models["suggestion"]["reason"]
    assert c.get("/api/ai/banner").json()["banner"] is None  # a single test never raises the banner


def test_bad_requests(c):
    assert c.post("/api/ai/tests", json={"names": []}).status_code == 400
    assert c.post("/api/ai/tests", json={"names": ["nope:1b"]}).status_code == 404
    assert c.post("/api/ai/tests", json={"names": ["bge-m3:latest"]}).status_code == 400
    assert c.post("/api/ai/tests", json={"names": ["a", "b"], "kind": "one"}).status_code == 400


def test_only_one_test_at_a_time(c, fake):
    gate = {"open": False}

    async def held(payload, request):
        while not gate["open"]:
            await asyncio.sleep(0.01)
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = held
    assert c.post("/api/ai/tests", json={"names": ["gemma4:latest"]}).status_code == 200
    time.sleep(0.2)
    second = c.post("/api/ai/tests", json={"names": ["llama3.1:8b"]})
    assert second.status_code == 409 and "Testing gemma4:latest" in second.json()["detail"]
    st = c.get("/api/ai/tests").json()
    assert st["busy"] and st["active"]["models"][0]["status"] == "running"
    gate["open"] = True
    wait_idle(c)
    assert c.post("/api/ai/tests", json={"names": ["llama3.1:8b"]}).status_code == 200
    wait_idle(c)


def test_test_all_raises_the_banner_when_something_is_better(c, fake):
    def llama_worse(payload, request):
        a = perfect_answer(payload)
        if payload["model"] == "llama3.1:8b":
            v = json.loads(a["response"])
            v["recommended_action"] = "restart" if v["recommended_action"] == "none" else "none"
            a["response"] = json.dumps(v)
        return a
    fake.servers["thor"].generate_handler = llama_worse
    plan = c.get("/api/ai/tests/plan").json()
    assert [m["name"] for m in plan["models"]][0] == "llama3.1:8b"  # the current model goes first
    assert all(m["ticked"] for m in plan["models"]) and plan["total_s"] > 0
    assert "bge-m3:latest" not in [m["name"] for m in plan["models"]]
    c.post("/api/ai/tests", json={"names": [m["name"] for m in plan["models"]], "kind": "all"})
    wait_idle(c)
    b = c.get("/api/ai/banner").json()["banner"]
    assert b["type"] == "better" and b["name"] == "gemma4:latest" and "llama3.1:8b" in b["text"]
    assert c.get("/api/ai/banner").json()["banner"] is not None  # stays until the user acts
    c.post("/api/ai/banner/dismiss", json={"run_id": b["run_id"]})
    assert c.get("/api/ai/banner").json()["banner"] is None
    assert c.get("/api/ai/models").json()["model"] == "llama3.1:8b"  # never switched on its own


def test_test_all_says_when_the_current_model_is_best(c):
    c.post("/api/ai/tests", json={"names": ["llama3.1:8b", "gemma4:latest"], "kind": "all"})
    wait_idle(c)
    b = c.get("/api/ai/banner").json()["banner"]
    assert b["type"] == "best" and "best fit" in b["text"]


def test_cancel_and_pause(c, fake):
    gate = {"open": False}

    async def held(payload, request):
        while not gate["open"]:
            await asyncio.sleep(0.01)
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = held
    run_id = c.post("/api/ai/tests", json={"names": ["gemma4:latest", "llama3.1:8b"], "kind": "all"}).json()["run_id"]
    time.sleep(0.2)
    assert c.post(f"/api/ai/tests/{run_id}/pause").status_code == 200
    gate["open"] = True
    time.sleep(0.3)
    st = c.get("/api/ai/tests").json()
    assert st["busy"] and st["active"]["status"] == "paused"
    assert {m["status"] for m in st["active"]["models"]} == {"queued"}
    assert c.post("/api/ai/tests", json={"names": ["gemma4:latest"]}).status_code == 409
    assert c.post(f"/api/ai/tests/{run_id}/resume").status_code == 200
    wait_idle(c)
    assert c.get("/api/ai/tests").json()["results"]["llama3.1:8b"]["status"] == "done"

    gate["open"] = False
    run_id = c.post("/api/ai/tests", json={"names": ["gemma4:latest", "llama3.1:8b"], "kind": "all"}).json()["run_id"]
    time.sleep(0.2)
    assert c.post(f"/api/ai/tests/{run_id}/cancel").status_code == 200
    gate["open"] = True
    st = wait_idle(c)
    with db.session() as conn:
        statuses = {r["status"] for r in conn.execute("SELECT status FROM model_tests WHERE run_id = ?", (run_id,))}
        assert statuses == {"cancelled"}
        assert conn.execute("SELECT banner FROM test_runs WHERE id = ?", (run_id,)).fetchone()["banner"] == "none"


def test_new_models_are_tested_automatically(c, fake):
    fake.servers["thor"].tags.append(tag("granite4.2:8b", 5.3, "granite", "8.0B"))
    c.get("/api/ai/models")
    st = wait_idle(c)
    assert st["results"]["granite4.2:8b"]["status"] == "done"
    assert "gemma4:latest" not in st["results"]  # only the new one


def test_new_models_wait_their_turn_and_can_be_switched_off(c, fake):
    from dockllama import settings
    with db.session() as conn:
        settings.set(conn, "ai.auto_test_new", False)
    fake.servers["thor"].tags.append(tag("granite4.2:8b", 5.3, "granite", "8.0B"))
    c.get("/api/ai/models")
    time.sleep(0.2)
    assert "granite4.2:8b" not in c.get("/api/ai/tests").json()["results"]


def test_unreachable_server_pauses_the_run(c, fake):
    def gone(payload, request):
        raise httpx.ConnectError("refused", request=request)
    fake.servers["thor"].generate_handler = gone
    c.post("/api/ai/tests", json={"names": ["gemma4:latest"]})
    time.sleep(0.5)
    st = c.get("/api/ai/tests").json()
    assert st["active"]["status"] == "paused" and "stopped answering" in st["active"]["note"]


def test_a_restart_carries_on_where_it_left_off(data_dir, fake):
    db.migrate()
    with db.session() as conn, db.transaction(conn):
        sid = ai_servers.connect(conn, "http://thor:11434", "0.40.17", [], None)
        models = asyncio.run(ollama.OllamaClient("http://thor:11434").models())
        ai_servers.save_models(conn, sid, models, [])
        run_id = model_tests.create_run(conn, sid, ["gemma4:latest", "llama3.1:8b"], "all", "watch")
        conn.execute("UPDATE model_tests SET status = 'running' WHERE run_id = ? AND position = 0", (run_id,))
    from fastapi.testclient import TestClient

    from dockllama.app import create_app
    with TestClient(create_app(), base_url="http://testserver") as c:
        c.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
        st = wait_idle(c)
        assert {r["status"] for r in st["results"].values()} == {"done"} and len(st["results"]) == 2
        with db.session() as conn:
            assert conn.execute("SELECT mode FROM test_runs WHERE id = ?", (run_id,)).fetchone()["mode"] == "background"


def test_run_tonight_is_scheduled_not_started(c):
    r = c.post("/api/ai/tests", json={"names": ["gemma4:latest"], "kind": "all", "tonight": True})
    assert r.status_code == 200
    time.sleep(0.2)
    st = c.get("/api/ai/tests").json()
    assert st["scheduled"]["status"] == "scheduled" and st["scheduled"]["start_after"] > time.time()
    assert not st["busy"]
    assert c.post("/api/ai/tests", json={"names": ["gemma4:latest"], "kind": "all", "tonight": True}).status_code == 409
    assert c.post(f"/api/ai/tests/{st['scheduled']['run_id']}/cancel").status_code == 200


def test_a_bad_model_is_never_called_better(c, fake):
    def broken(payload, request):
        return {"response": "nope", "done": True, "done_reason": "stop", "eval_count": 0}
    fake.servers["thor"].generate_handler = broken
    c.post("/api/ai/tests", json={"names": ["gemma4:latest"], "kind": "all"})
    st = wait_idle(c)
    assert st["results"]["gemma4:latest"]["score"] == 0.0
    b = c.get("/api/ai/banner").json()["banner"]
    assert b["type"] == "best" and "None of the tested models" in b["text"]
    assert c.get("/api/ai/models").json()["suggestion"]["name"] != "gemma4:latest"  # 0 points: not suggested


def test_untested_current_model_is_compared_with_its_catalog_score(c, fake):
    # gemma4 scores 100 here; llama3.1 (current, untested here) has a catalog score of 94.9
    c.post("/api/ai/tests", json={"names": ["gemma4:latest"], "kind": "all"})
    wait_idle(c)
    b = c.get("/api/ai/banner").json()["banner"]
    assert b["type"] == "better" and "hasn't been tested on this server" in b["text"]


def test_retry_can_be_switched_off(fake):
    def prose(payload, request):
        return {"response": "It looks fine to me.", "done": True, "done_reason": "stop"}
    fake.servers["thor"].generate_handler = prose
    r = _run(run_model_test(ollama.OllamaClient("http://thor:11434"), _model(), {"retry": False}, Progress(1, "x"),
                            keep_loaded=False))
    assert r["score"] == 0.0 and len(fake.servers["thor"].generate_calls) == 8
