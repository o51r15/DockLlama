"""AI instructions: a read-only Default plus alternatives, exactly one in use; the answer
format is always added; custom-prompt tests never feed suggestions."""

import asyncio

from dockllama import db, evaluate, prompts
from dockllama.evaluate import ANSWER_FORMAT, DEFAULT_INSTRUCTIONS, SYSTEM_PROMPT, system_prompt

from .fake_ollama import perfect_answer
from .test_model_tests import c, fake, wait_idle  # noqa: F401  (fixtures)


def test_default_is_sent_unchanged_and_custom_gets_the_locked_format():
    assert system_prompt() == SYSTEM_PROMPT  # existing scores stay comparable
    assert "## Response format" not in DEFAULT_INSTRUCTIONS and "## Rules" in DEFAULT_INSTRUCTIONS
    full = system_prompt("Judge containers strictly. " * 5)
    assert full.endswith(ANSWER_FORMAT) and '"what_to_do"' in full


def test_create_edit_use_and_delete(c):
    p = c.get("/api/prompts").json()
    assert p["active"] == 0 and p["prompts"] == [] and p["default"] == DEFAULT_INSTRUCTIONS
    one = c.post("/api/prompts", json={"name": "strict"}).json()
    assert one["number"] == 1 and one["text"] == DEFAULT_INSTRUCTIONS and one["label"] == "Prompt 1 (strict)"
    two = c.post("/api/prompts", json={"text": "Be lenient about warnings. " * 4}).json()
    assert two["number"] == 2
    assert c.post("/api/prompts", json={"text": "too short"}).status_code == 400

    assert c.put("/api/prompts/active", json={"id": two["id"]}).json()["active"] == two["id"]
    with db.session() as conn:
        label, system = prompts.active_system(conn)
    assert label == "Prompt 2" and system.startswith("Be lenient") and system.endswith(ANSWER_FORMAT)

    # renaming keeps the revision; changing the text bumps it
    assert c.put(f"/api/prompts/{one['id']}", json={"name": "x", "text": one["text"]}).json()["revision"] == 1
    assert c.put(f"/api/prompts/{one['id']}", json={"name": "x", "text": one["text"] + " Extra."}).json()["revision"] == 2

    # deleting the one in use goes back to the Default; a new prompt takes the lowest free number
    assert c.delete(f"/api/prompts/{two['id']}").json()["active"] == 0
    assert c.post("/api/prompts", json={}).json()["number"] == 2
    assert c.put("/api/prompts/active", json={"id": 999}).status_code == 400


def test_prompt_tests_use_the_prompt_and_never_feed_suggestions(c, fake):
    c.get("/api/ai/models")
    sent = []

    def record(payload, request):
        sent.append(payload.get("system", ""))
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = record
    pid = c.post("/api/prompts", json={"text": "Prompt under test. " * 5}).json()["id"]
    r = c.post(f"/api/prompts/{pid}/test", json={"names": ["gemma4:latest"]})
    assert r.status_code == 200
    wait_idle(c)
    assert sent and all(s.startswith("Prompt under test.") and s.endswith(ANSWER_FORMAT) for s in sent)

    res = {m["name"]: m for m in c.get(f"/api/prompts/{pid}/results").json()["models"]}
    assert res["gemma4:latest"]["this"]["status"] == "done" and res["gemma4:latest"]["default"] is None
    # nothing outside the prompt screen sees it
    st = c.get("/api/ai/tests").json()
    assert "gemma4:latest" not in st["results"]
    assert c.get("/api/ai/banner").json()["banner"] is None
    assert c.get(f"/api/ai/tests/history?name=gemma4:latest").json().get("history", []) == []

    # editing the prompt marks the result as from an earlier version
    text = c.get("/api/prompts").json()["prompts"][0]["text"]
    c.put(f"/api/prompts/{pid}", json={"text": text + " Changed."})
    res = {m["name"]: m for m in c.get(f"/api/prompts/{pid}/results").json()["models"]}
    assert res["gemma4:latest"]["this"]["outdated"] is True


def test_normal_tests_still_use_the_default_while_a_custom_prompt_is_in_use(c, fake):
    c.get("/api/ai/models")
    sent = []

    def record(payload, request):
        sent.append(payload.get("system", ""))
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = record
    pid = c.post("/api/prompts", json={"text": "Not for model tests. " * 5}).json()["id"]
    c.put("/api/prompts/active", json={"id": pid})
    c.post("/api/ai/tests", json={"names": ["gemma4:latest"]})
    wait_idle(c)
    assert sent and all(s == SYSTEM_PROMPT for s in sent)


def test_a_prompt_being_tested_cant_be_deleted(c, fake):
    c.get("/api/ai/models")
    gate = {"open": False}

    async def hold(payload, request):
        while not gate["open"]:
            await asyncio.sleep(0.01)
        return perfect_answer(payload)
    fake.servers["thor"].generate_handler = hold
    pid = c.post("/api/prompts", json={"text": "Held prompt text here. " * 4}).json()["id"]
    c.post(f"/api/prompts/{pid}/test", json={"names": ["gemma4:latest"]})
    assert c.delete(f"/api/prompts/{pid}").status_code == 409
    assert c.post(f"/api/prompts/{pid}/test", json={"names": ["gemma4:latest"]}).status_code == 409  # one at a time
    gate["open"] = True
    wait_idle(c)
    assert c.delete(f"/api/prompts/{pid}").status_code == 200
