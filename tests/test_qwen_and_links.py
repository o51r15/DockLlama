"""Qwen-based models are labelled and can be left out of suggestions; links never leave the AI."""

import json

from dockllama import db, evaluate
from dockllama.links import REMOVED, strip_links
from dockllama.ollama import is_qwen_based

from .fake_ollama import perfect_answer, tag
from .test_model_tests import c, fake, wait_idle  # noqa: F401  (fixtures)


def test_qwen_detection_uses_name_family_architecture_and_base_model():
    assert is_qwen_based("qwen3:8b", {}, {})
    assert is_qwen_based("glimmer:latest", {"family": "qwen2"}, {})
    assert is_qwen_based("glimmer:latest", {"families": ["llama", "qwen3moe"]}, {})
    assert is_qwen_based("glimmer:latest", {}, {"general.architecture": "qwen3"})
    assert is_qwen_based("glimmer:latest", {}, {"general.basename": "Qwen3-8B"})
    assert is_qwen_based("glimmer:latest", {}, {"general.base_model.0.name": "Qwen3 8B"})
    assert not is_qwen_based("llama3.1:8b", {"family": "llama"}, {"general.architecture": "llama"})


def test_links_are_removed_but_file_names_and_ips_stay():
    cases = {
        "See https://qwen.ai/help?x=1. Then restart.": f"See {REMOVED}. Then restart.",
        "Visit www.alibaba.com/upload, now": f"Visit {REMOVED}, now",
        "Read [the docs](http://x.cn/a).": "Read the docs.",
        "send to files.aliyuncs.com/u/123": f"send to {REMOVED}",
        "mail admin@example.com.": f"mail {REMOVED}.",
        "postgres://user@db:5432/x refused": f"{REMOVED} refused",
    }
    for text, want in cases.items():
        assert strip_links(text) == want, text
    keep = "config.yml is missing; entrypoint.sh failed in app.py; can't reach 192.168.1.5:5432; log.info"
    assert strip_links(keep) == keep


def test_parsed_answers_have_no_links():
    raw = json.dumps({"status": "degraded", "health_score": 60, "confidence": 80,
                      "summary": "Upload logs to https://evil.example.com/x for help.",
                      "what_to_do": "See www.qwen.ai", "restart_would_help": False,
                      "restart_reasoning": "per evil.top/a", "recommended_action": "none"})
    v = evaluate.parse_answer(raw)
    assert "http" not in v["summary"] and REMOVED in v["summary"]
    assert v["what_to_do"] == f"See {REMOVED}" and v["restart_reasoning"] == f"per {REMOVED}"


def _add_glimmer(c, fake):
    fake.servers["thor"].tags = fake.servers["thor"].tags + [tag("glimmer:latest", 5.0, "glimmer", "8.2B")]
    return c.get("/api/ai/models").json()


def test_qwen_models_are_labelled_and_stored(c, fake):
    models = {m["name"]: m for m in _add_glimmer(c, fake)["models"]}
    assert models["glimmer:latest"]["qwen_based"] and not models["llama3.1:8b"]["qwen_based"]
    with db.session() as conn:
        row = conn.execute("SELECT qwen_based FROM models WHERE name = 'glimmer:latest'").fetchone()
    assert row["qwen_based"] == 1


def test_exclude_setting_keeps_qwen_out_of_suggestions_and_the_banner(c, fake):
    c.put("/api/ai/auto-test", json={"enabled": False})  # don't race an automatic test of the new model
    _add_glimmer(c, fake)

    def only_glimmer_perfect(payload, request):  # glimmer is clearly the best model
        a = perfect_answer(payload)
        if payload["model"] != "glimmer:latest":
            v = json.loads(a["response"])
            v["recommended_action"] = "restart" if v["recommended_action"] == "none" else "none"
            a["response"] = json.dumps(v)
        return a
    fake.servers["thor"].generate_handler = only_glimmer_perfect
    assert c.get("/api/ai/exclude-qwen").json() == {"enabled": False}  # off by default

    c.post("/api/ai/tests", json={"names": ["llama3.1:8b", "gemma4:latest", "glimmer:latest"], "kind": "all"})
    wait_idle(c)
    b = c.get("/api/ai/banner").json()["banner"]
    assert b["type"] == "better" and b["name"] == "glimmer:latest"  # label only: still suggested
    assert c.get("/api/ai/models").json()["suggestion"]["name"] == "glimmer:latest"
    c.post("/api/ai/banner/dismiss", json={"run_id": b["run_id"]})

    assert c.put("/api/ai/exclude-qwen", json={"enabled": True}).json() == {"enabled": True}
    assert c.get("/api/ai/models").json()["suggestion"]["name"] != "glimmer:latest"
    c.post("/api/ai/tests", json={"names": ["llama3.1:8b", "gemma4:latest", "glimmer:latest"], "kind": "all"})
    st = wait_idle(c)
    assert st["results"]["glimmer:latest"]["status"] == "done"  # still tested
    b = c.get("/api/ai/banner").json()["banner"]
    assert b is None or b["name"] != "glimmer:latest"
    # it can still be chosen by hand
    assert c.put("/api/ai/model", json={"name": "glimmer:latest"}).status_code == 200
