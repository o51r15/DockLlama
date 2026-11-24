import asyncio

import pytest

from dockllama import advanced, catalog, ollama
from dockllama.ollama import ModelDetails, OllamaError, normalize_url

from .fake_ollama import SHOW, FakeOllama, FakeServer, tag


@pytest.fixture
def fake(monkeypatch):
    f = FakeOllama()
    f.servers["thor"] = FakeServer()
    monkeypatch.setattr(ollama, "TEST_TRANSPORT", f.transport())
    return f


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("typed,url", [
    ("192.168.1.125", "http://192.168.1.125:11434"),
    ("thor", "http://thor:11434"),
    ("thor:11500", "http://thor:11500"),
    ("http://thor:11434/", "http://thor:11434"),
    ("https://ai.example.com", "https://ai.example.com:11434"),
    ("  10.0.0.5  ", "http://10.0.0.5:11434"),
    ("[::1]", "http://[::1]:11434"),
])
def test_normalize_url(typed, url):
    assert normalize_url(typed) == url


@pytest.mark.parametrize("bad", ["", "   ", "ftp://x", "http://", "thor:notaport"])
def test_normalize_url_rejects(bad):
    with pytest.raises(OllamaError):
        normalize_url(bad)


def test_check_ok_too_old_not_ollama_unreachable(fake):
    assert run(ollama.OllamaClient("http://thor:11434").check()) == "0.40.17"
    fake.servers["old"] = FakeServer(version="0.6.8")
    with pytest.raises(OllamaError) as e:
        run(ollama.OllamaClient("http://old:11434").check())
    assert e.value.kind == "too_old" and "0.9.0" in str(e.value)
    fake.not_ollama.add("router")
    with pytest.raises(OllamaError) as e:
        run(ollama.OllamaClient("http://router:11434").check())
    assert e.value.kind == "not_ollama"
    with pytest.raises(OllamaError) as e:
        run(ollama.OllamaClient("http://nobody:11434").check())
    assert e.value.kind == "unreachable"


def test_dev_build_version_not_rejected(fake):
    fake.servers["dev"] = FakeServer(version="0.0.0")
    assert run(ollama.OllamaClient("http://dev:11434").check()) == "0.0.0"


def test_model_details_match_real_shapes(fake):
    models = {m.name: m for m in run(ollama.OllamaClient("http://thor:11434").models())}
    gpt = models["gpt-oss:20b"]
    assert gpt.thinking == "levels" and gpt.think_levels == ["low", "medium", "high"] and gpt.max_context == 131072
    assert models["gemma4:latest"].thinking == "toggle"
    assert models["llama3.1:8b"].thinking == "none"
    bge = models["bge-m3:latest"]
    assert not bge.usable and "Embedding" in bge.unusable_reason


def test_capability_without_details_is_a_toggle():
    m = ollama.details_from(tag("granite4.2:8b", 5.3, "granite", "8.8B"), SHOW["granite4.2:8b"])
    assert m.thinking == "toggle"


def test_cloud_models_unusable():
    t = tag("gpt-oss:120b-cloud", 0, "gpt-oss", "120B")
    t["remote_host"] = "https://ollama.com:443"
    m = ollama.details_from(t, SHOW["gpt-oss:20b"])
    assert not m.usable and "Cloud" in m.unusable_reason


def _model(name, thinking="none", levels=None, max_ctx=131072):
    return ModelDetails(name=name, digest="", modified_at="", size=5 * 10**9, family="", parameter_size="8B",
                        capabilities=["completion"], max_context=max_ctx, thinking=thinking, think_levels=levels or [])


def test_request_builder_think_rules():
    gpt = _model("gpt-oss:20b", "levels", ["low", "medium", "high"])
    p = ollama.build_generate_request(gpt, "hi", schema={"type": "object"})
    assert p["think"] == "low" and p["options"]["num_ctx"] == 8192 and p["format"] == {"type": "object"}
    assert "think" not in p["options"] and p["keep_alive"]
    gemma = ollama.build_generate_request(_model("gemma4", "toggle"), "hi")
    assert gemma["think"] is False and gemma["options"]["num_predict"] == 500
    llama = ollama.build_generate_request(_model("llama3.1", "none"), "hi")
    assert "think" not in llama
    on = ollama.build_generate_request(_model("gemma4", "toggle"), "hi", settings=ollama.RequestSettings(think=True))
    assert on["think"] is True and on["options"]["num_predict"] == 2000  # room for thinking


def test_request_builder_caps_context_at_model_max():
    small = _model("tiny", max_ctx=4096)
    assert ollama.build_generate_request(small, "x")["options"]["num_ctx"] == 4096


def test_truncation_warnings():
    payload = {"options": {"num_ctx": 8192}}
    assert ollama.truncation_warnings(payload, {"done_reason": "length", "prompt_eval_count": 900}, 3600)
    assert ollama.truncation_warnings(payload, {"done_reason": "stop", "prompt_eval_count": 8100}, 40000)
    assert ollama.truncation_warnings(payload, {"done_reason": "stop", "prompt_eval_count": 300}, 20000)
    # The real Thor measurement: 3,751 characters -> 915 tokens, cache or not. No warning.
    assert ollama.truncation_warnings(payload, {"done_reason": "stop", "prompt_eval_count": 915}, 3751) == []


def test_fingerprint_survives_new_models_and_ignores_address():
    base = [tag("a", 1, "x", "1B", pulled="t1"), tag("b", 1, "x", "1B", pulled="t2")]
    fp = ollama.inventory_fingerprint(base)
    assert ollama.fingerprint_overlap(fp, ollama.inventory_fingerprint(base + [tag("c", 1, "x", "1B")])) == 1.0
    same_models_other_machine = [tag("a", 1, "x", "1B", pulled="t9"), tag("b", 1, "x", "1B", pulled="t8")]
    assert ollama.fingerprint_overlap(fp, ollama.inventory_fingerprint(same_models_other_machine)) == 0.0


def test_pull_streams_progress_and_reports_errors(fake):
    events = []

    async def go(name):
        async for e in ollama.OllamaClient("http://thor:11434").pull(name):
            events.append(e)

    run(go("granite4.2:8b"))
    assert events[-1]["status"] == "success" and any(e.get("completed") == 500 for e in events)
    with pytest.raises(OllamaError, match="no model called"):
        run(go("nope:1b"))


def test_discover_finds_only_real_ollama(fake, monkeypatch):
    monkeypatch.setattr(ollama, "DISCOVERY_CANDIDATES", ["http://host.docker.internal:11434", "http://ollama:11434"])
    fake.servers["host.docker.internal"] = FakeServer()
    fake.not_ollama.add("ollama")
    assert [f["url"] for f in run(ollama.discover())] == ["http://host.docker.internal:11434"]


# --- best guess / suggestions ------------------------------------------------------

def test_best_guess_prefers_loaded_model_when_nearly_as_good(fake):
    models = run(ollama.OllamaClient("http://thor:11434").models())
    s = catalog.best_guess(models, "gpu16", loaded={"gpt-oss:20b"})
    assert s.name == "gpt-oss:20b" and "already loaded" in s.reason


def test_best_guess_respects_hardware(fake):
    models = run(ollama.OllamaClient("http://thor:11434").models())
    s = catalog.best_guess(models, "gpu8", loaded=set())
    assert s.name in {"gemma4:latest", "llama3.1:8b"} and "fits your 8 gb" in s.reason.lower()
    assert catalog.best_guess(models, "gpu8", loaded={"gpt-oss:20b"}).name != "gpt-oss:20b"  # too big for 8 GB


def test_best_guess_never_picks_embedding_or_nothing():
    only_embed = [ollama.details_from(tag("bge-m3:latest", 1.2, "bert", "566M"), SHOW["bge-m3:latest"])]
    assert catalog.best_guess(only_embed, "gpu16") is None


def test_download_suggestions_fit_and_skip_installed(fake):
    models = run(ollama.OllamaClient("http://thor:11434").models())
    names = [d["name"] for d in catalog.download_suggestions(models, "gpu8")]
    assert "gpt-oss:20b" not in names and "gemma4:latest" not in names and names
    assert all(d["size_gb"] < 8 for d in catalog.download_suggestions(models, "gpu8"))
    cpu = [d["name"] for d in catalog.download_suggestions([], "none")]
    assert "granite4.2:8b" not in cpu  # dense and not marked CPU-friendly


def test_catalog_has_no_qwen_based_models():
    assert all(not m["name"].startswith(("qwen", "ornith")) for m in catalog.catalog()["models"])


# --- Advanced settings validation ---------------------------------------------------

def test_advanced_validation_follows_model_capabilities():
    llama, gemma = _model("llama", "none"), _model("gemma", "toggle")
    gpt = _model("gpt", "levels", ["low", "medium", "high"])
    with pytest.raises(advanced.AdvancedError, match="doesn't think"):
        advanced.validate(llama, {"think": False})
    with pytest.raises(advanced.AdvancedError, match="on or off"):
        advanced.validate(gemma, {"think": "low"})
    with pytest.raises(advanced.AdvancedError, match="one of"):
        advanced.validate(gpt, {"think": True})
    assert advanced.validate(gpt, {"think": "high"}) == {"think": "high"}


def test_context_slider_limited_to_model_max():
    eight_k = _model("tiny", max_ctx=8192)
    opts = advanced.options_for(eight_k)
    assert opts["context_max"] == 8192 and opts["context_steps"] == [4096, 8192]
    with pytest.raises(advanced.AdvancedError):
        advanced.validate(eight_k, {"num_ctx": 16384})
    with pytest.raises(advanced.AdvancedError):
        advanced.validate(eight_k, {"num_ctx": 2048})
    assert advanced.validate(eight_k, {"num_ctx": 8192}) == {"num_ctx": 8192}
    assert advanced.options_for(_model("big", max_ctx=262144))["context_steps"][-1] == 262144


def test_advanced_other_fields_and_warnings():
    gemma = _model("gemma", "toggle")
    with pytest.raises(advanced.AdvancedError):
        advanced.validate(gemma, {"temperature": 2})
    with pytest.raises(advanced.AdvancedError):
        advanced.validate(gemma, {"bogus": 1})
    clean = advanced.validate(gemma, {"think": True, "num_predict": 500, "keep_alive": "always", "temperature": None})
    assert clean == {"think": True, "num_predict": 500, "keep_alive": "always"}
    assert any("cut off" in w for w in advanced.warnings_for(gemma, clean))
    req = advanced.to_request(clean)
    assert req.keep_alive == -1 and req.think is True
    assert advanced.to_request({"keep_alive": "unload"}).keep_alive == 0
    assert advanced.to_request({}, interval_minutes=10).keep_alive == "20m"


def test_discover_shows_the_same_server_once(fake, monkeypatch):
    monkeypatch.setattr(ollama, "DISCOVERY_CANDIDATES", ["http://host.docker.internal:11434", "http://172.17.0.1:11434"])
    same = FakeServer()
    fake.servers["host.docker.internal"] = same
    fake.servers["172.17.0.1"] = same
    assert [f["url"] for f in run(ollama.discover())] == ["http://host.docker.internal:11434"]
    fake.servers["172.17.0.1"] = FakeServer(tags=[tag("other", 1, "x", "1B", pulled="z")])
    assert len(run(ollama.discover())) == 2


def test_no_false_thinking_warning_at_default_length():
    gpt = _model("gpt", "levels", ["low", "medium", "high"])
    assert not any("cut off" in w for w in advanced.warnings_for(gpt, {"think": "medium"}))
    assert any("cut off" in w for w in advanced.warnings_for(gpt, {"think": "medium", "num_predict": 600}))


def test_default_server_names():
    from dockllama.ai_servers import default_name
    assert default_name("http://host.docker.internal:11434") == "Ollama on this machine"
    assert default_name("http://192.168.1.125:11434") == "192.168.1.125"


def test_levels_model_that_can_switch_off():
    from dockllama import ollama
    show = {"capabilities": ["completion", "thinking"], "model_info": {"general.architecture": "x", "x.context_length": 131072},
            "thinking": {"values": [False, "low", "medium", "high", "max"], "default": "high"}}
    m = ollama.details_from({"name": "muse", "digest": "d", "modified_at": "t", "size": 1, "details": {}}, show)
    assert m.thinking == "levels" and m.think_levels == ["off", "low", "medium", "high", "max"]
    assert ollama.recommended_think(m) is False
    assert ollama.build_generate_request(m, "hi")["think"] is False
    assert advanced.validate(m, {"think": "off"})["think"] is False
    assert advanced.validate(m, {"think": False})["think"] is False
    assert advanced.validate(m, {"think": "max"})["think"] == "max"
