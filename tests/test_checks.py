import asyncio
import json
import time

import httpx
import pytest

from dockllama import ai_servers, checks, db, machines, ollama, rules, settings
from dockllama.rules import Facts

from .fake_ollama import FakeOllama, FakeServer, tag

# --- rules ----------------------------------------------------------------------------------


def test_rules_plain_cases():
    ev = rules.evaluate
    assert ev(Facts("a", "running"), None).status is None
    assert ev(Facts("a", "exited", exit_code=0), None).status == "stopped"
    assert ev(Facts("a", "exited", exit_code=137, stopped_by_user=True, events=["kill", "stop", "die"]), None).status == "stopped"
    killed = ev(Facts("a", "exited", exit_code=137, events=["kill", "die"]), None)
    assert killed.status == "critical" and "SIGKILL" in killed.reason
    assert ev(Facts("a", "exited", exit_code=3), None).status == "critical"
    assert ev(Facts("a", "exited", exit_code=143), None).status == "stopped"  # no event log: SIGTERM = normal stop
    oom = ev(Facts("a", "exited", exit_code=137, oom_killed=True), None)
    assert oom.status == "critical" and "memory limit" in oom.what_to_do
    sick = ev(Facts("a", "running", health="unhealthy", health_streak=4, health_output="curl: (7) refused"), None)
    assert sick.status == "unhealthy" and "refused" in sick.reason
    loop = ev(Facts("a", "running", restart_count=7), Facts("a", "running", restart_count=4))
    assert loop.status == "critical" and loop.rule == "crash_loop"
    assert ev(Facts("a", "restarting", restart_count=2, oom_killed=True), None).rule == "oom_loop"
    assert ev(Facts("a", "running", restart_count=5), Facts("a", "running", restart_count=4)).status == "degraded"
    assert ev(Facts("a", "running", restart_count=5, stopped_by_user=True), Facts("a", "running", restart_count=4)).status is None
    assert ev(Facts("a", "paused"), None).status == "stopped" and not ev(Facts("a", "created"), None).ask_ai
    assert rules.worse("healthy", "unhealthy") == "unhealthy" and rules.worse("critical", "unknown") == "critical"


# --- the engine -----------------------------------------------------------------------------


class FakeDocker:
    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.logs_by_name: dict[str, list[tuple[float, str]]] = {}
        self.events_by_id: dict[str, list[str]] = {}

    def add(self, name, state="running", exit_code=0, oom=False, restarts=0, health=None, log_driver="json-file"):
        self.containers[name] = {
            "Id": "id-" + name, "Name": "/" + name, "RestartCount": restarts,
            "Config": {"Image": "busybox", "Labels": {}},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "LogConfig": {"Type": log_driver}},
            "State": {"Status": state, "ExitCode": exit_code, "OOMKilled": oom, "StartedAt": "2026-11-24T01:00:00Z",
                      "FinishedAt": "2026-11-24T02:00:00Z" if state == "exited" else "0001-01-01T00:00:00Z",
                      **({"Health": {"Status": health, "FailingStreak": 3, "Log": [{"Output": "refused"}]}} if health else {})},
        }
        self.logs_by_name.setdefault(name, [])

    def log(self, name, *lines):
        base = time.time()
        for i, line in enumerate(lines):
            self.logs_by_name[name].append((base + i * 0.001, line))

    async def aversion(self):
        return "29.0.0"

    async def alist_containers(self):
        out = []
        for a in self.containers.values():
            out.append(machines.ContainerInfo(
                id=a["Id"], name=a["Name"][1:], image="busybox", state=a["State"]["Status"],
                health=(a["State"].get("Health") or {}).get("Status"), exit_code=a["State"]["ExitCode"],
                oom_killed=a["State"]["OOMKilled"], restart_count=a["RestartCount"], restart_policy="unless-stopped",
                started_at=None, finished_at=None, log_driver="json-file", compose_project=None, compose_service=None,
                is_self=False, is_ollama=False))
        return out

    async def ainspect(self, name):
        if name not in self.containers:
            raise machines.MachineError("That container doesn't exist any more.")
        return json.loads(json.dumps(self.containers[name]))

    async def alogs(self, name, since, tail):
        lines = [x for x in self.logs_by_name.get(name, []) if not since or x[0] > since]
        return lines[-tail:]

    async def astats(self, name):
        return 3.0, 20.0

    async def aevents(self, cid, since, until):
        return self.events_by_id.get(cid, [])


@pytest.fixture
def docker(monkeypatch):
    d = FakeDocker()
    monkeypatch.setattr(machines, "get_machine", lambda mid: d)
    return d


@pytest.fixture
def fake(monkeypatch):
    f = FakeOllama()
    f.servers["thor"] = FakeServer(tags=[tag("llama3.1:8b", 4.9, "llama", "8.0B")], loaded=[])
    monkeypatch.setattr(ollama, "TEST_TRANSPORT", f.transport())
    return f


def verdict(status="healthy", score=95, summary="It is working normally with no problems.", todo="Nothing.", restart=False):
    def answer(payload, request):
        return {"response": json.dumps({"status": status, "health_score": score, "confidence": 90, "summary": summary,
                                        "what_to_do": todo, "restart_would_help": restart, "recommended_action": "none"}),
                "done": True, "done_reason": "stop", "prompt_eval_count": 1500, "eval_count": 60}
    return answer


def setup_db(watch: list[str], with_ai: bool):
    db.migrate()
    with db.session() as conn, db.transaction(conn):
        settings.set(conn, "setup.complete", True)
        if with_ai:
            sid = ai_servers.connect(conn, "http://thor:11434", "0.40.17", [], None)
            models = asyncio.run(ollama.OllamaClient("http://thor:11434").models())
            ai_servers.save_models(conn, sid, models, [])
            conn.execute("UPDATE ai_servers SET model = 'llama3.1:8b' WHERE id = ?", (sid,))


def watch_all():
    with db.session() as conn:
        conn.execute("UPDATE containers SET monitored = 1")


def run(**kw):
    return {r["name"]: r for r in asyncio.run(checks.run_checks(**kw))}


def first_round(docker):
    run()  # learns the containers
    watch_all()
    return run()


def state(name):
    with db.session() as conn:
        return dict(conn.execute("SELECT s.* FROM container_state s JOIN containers c ON c.id = s.container_id "
                                 "WHERE c.name = ?", (name,)).fetchone())


def test_without_ai_rules_decide(data_dir, docker):
    setup_db([], with_ai=False)
    docker.add("web"); docker.add("job", "exited", 0); docker.add("killed", "exited", 137)
    docker.add("stopped", "exited", 137); docker.events_by_id["id-stopped"] = ["kill", "stop", "die"]
    docker.add("oom", "exited", 137, oom=True); docker.add("sick", health="unhealthy")
    r = first_round(docker)
    assert {n: x["status"] for n, x in r.items()} == {
        "web": "healthy", "job": "stopped", "killed": "critical", "stopped": "stopped", "oom": "critical", "sick": "unhealthy"}
    assert r["web"]["source"] == "no_ai" and "No AI is connected" in r["web"]["summary"]
    # a second round with nothing new adds no history rows
    with db.session() as conn:
        before = conn.execute("SELECT COUNT(*) n FROM checks").fetchone()["n"]
    run()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM checks").fetchone()["n"] == before
        changes = conn.execute("SELECT COUNT(*) n FROM events WHERE kind = 'status_change'").fetchone()["n"]
    assert changes == 6


def test_with_ai_and_skip_unchanged(data_dir, docker, fake):
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict()
    docker.add("web")
    docker.log("web", "INFO started", "INFO GET / 200", "INFO GET /health 200")
    r = first_round(docker)
    assert r["web"]["status"] == "healthy" and r["web"]["source"] == "ai" and r["web"]["what_to_do"] == ""
    calls = len(fake.servers["thor"].generate_calls)
    assert calls == 1
    sent = fake.servers["thor"].generate_calls[0]
    assert "Container: web" in sent["prompt"] and "Recent Tail" in sent["prompt"] and sent["format"]["type"] == "object"
    run()  # nothing new: no AI call
    assert len(fake.servers["thor"].generate_calls) == calls and state("web")["status"] == "healthy"
    docker.log("web", "ERROR db connection lost")
    run()  # new log lines: asked again, and only the new lines are sent
    assert len(fake.servers["thor"].generate_calls) == calls + 1
    assert "INFO started" not in fake.servers["thor"].generate_calls[-1]["prompt"]
    assert "db connection lost" in fake.servers["thor"].generate_calls[-1]["prompt"]


def test_checks_use_the_prompt_in_use(data_dir, docker, fake):
    """Default is sent as is; after picking Prompt 1, checks send its text plus the locked answer
    format, and the evidence says which instructions judged the container."""
    from dockllama import prompts as prompts_mod
    from dockllama.evaluate import ANSWER_FORMAT, SYSTEM_PROMPT
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict(status="degraded", score=60, summary="Some warnings.")
    docker.add("web")
    docker.log("web", "WARN slow")
    first_round(docker)
    assert fake.servers["thor"].generate_calls[-1]["system"] == SYSTEM_PROMPT
    with db.session() as conn, db.transaction(conn):
        pid = prompts_mod.create(conn, "Only call a container unhealthy if users would notice. " * 3)
        prompts_mod.use(conn, pid)
    docker.log("web", "WARN slower")
    run()
    sent = fake.servers["thor"].generate_calls[-1]["system"]
    assert sent.startswith("Only call a container unhealthy") and sent.endswith(ANSWER_FORMAT)
    with db.session() as conn:
        ev = json.loads(conn.execute("SELECT evidence FROM checks ORDER BY id DESC LIMIT 1").fetchone()["evidence"])
    assert ev["instructions"] == "Prompt 1"


def test_rules_win_when_worse_and_ai_failure_is_never_healthy(data_dir, docker, fake):
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict()
    docker.add("sick", health="unhealthy"); docker.log("sick", "INFO all good")
    r = first_round(docker)
    assert r["sick"]["status"] == "unhealthy" and "health check" in r["sick"]["summary"]

    def down(payload, request):
        raise httpx.ConnectError("refused", request=request)
    fake.servers["thor"].generate_handler = down
    docker.add("web"); docker.log("web", "INFO hi")
    run(); watch_all()
    r = run()
    assert r["web"]["status"] == "unknown" and r["web"]["summary"].startswith("Couldn't check")
    assert state("web")["unknown_streak"] == 1
    docker.log("web", "INFO again")
    run()
    assert state("web")["unknown_streak"] == 2


def test_noisy_but_fine_is_working_fine(data_dir, docker, fake):
    setup_db([], with_ai=True)
    seen = {}

    def judge(payload, request):
        seen["prompt"] = payload["prompt"]
        return verdict()(payload, request)
    fake.servers["thor"].generate_handler = judge
    docker.add("bitmagnet")
    docker.log("bitmagnet", *(["INFO indexed 140 torrents"] * 5 + ["ERROR dial tcp 1.2.3.4:6881: i/o timeout"] * 8))
    run(); watch_all()
    with db.session() as conn:
        conn.execute("UPDATE containers SET known_patterns = ?, notes_for_ai = ? WHERE name = 'bitmagnet'",
                     (json.dumps([{"pattern": "i/o timeout", "tag": "[ROUTINE: DHT network timeout]"}]),
                      "BitTorrent indexer. DHT timeouts are normal."))
    r = run()
    assert r["bitmagnet"]["status"] == "healthy"
    assert "8 (8 routine) ERROR" in seen["prompt"] and "Container-Specific Context: BitTorrent indexer" in seen["prompt"]


def test_secrets_never_reach_the_ai_or_the_database(data_dir, docker, fake):
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict("degraded", 60, "Login problems.", "Check the credentials.")
    docker.add("app"); docker.log("app", "ERROR login failed for postgres://admin:hunter2@db/app password=hunter2")
    run(); watch_all(); run()
    assert "hunter2" not in fake.servers["thor"].generate_calls[-1]["prompt"]
    with db.session() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM checks")])
    assert "hunter2" not in dump and "[redacted]" in dump


def test_stopped_containers_skip_the_ai(data_dir, docker, fake):
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict()
    docker.add("job", "exited", 0); docker.log("job", "INFO done")
    r = first_round(docker)
    assert r["job"]["status"] == "stopped" and fake.servers["thor"].generate_calls == []


def test_gone_container(data_dir, docker):
    setup_db([], with_ai=False)
    docker.add("tmp")
    first_round(docker)
    del docker.containers["tmp"]
    r = asyncio.run(checks.run_checks(container_ids=[1]))
    assert r[0]["status"] == "gone"


def test_prompt_is_trimmed_to_fit(data_dir):
    c = checks.Collected(row={"name": "big", "last_log_ts": 1.0, "ignore_patterns": "[]", "known_patterns": "[]"},
                         facts=Facts("big", "running"), lines=[f"ERROR thing {i} " + "x" * 1900 for i in range(300)])
    text, _ = checks.build_prompt(c, "", 10, 4096)
    assert len(text) <= int((4096 * 0.75 - checks.SYSTEM_PROMPT_TOKENS) * checks.CHARS_PER_TOKEN)
    assert "Recent Tail" in text


def test_check_now_and_overview_api(client, docker, fake):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    fake.servers["thor"].generate_handler = verdict("unhealthy", 20, "It can't reach its database.", "Check the database container.")
    assert client.post("/api/ai/connect", json={"address": "thor"}).json()["ok"]
    client.put("/api/ai/model", json={"name": "llama3.1:8b"})
    docker.add("web"); docker.log("web", "ERROR db unreachable")
    client.get("/api/machines/1/containers")
    with db.session() as conn:
        cid = conn.execute("SELECT id FROM containers WHERE name = 'web'").fetchone()["id"]
        conn.execute("UPDATE containers SET monitored = 1")
    assert client.get("/api/overview").json()["containers"][0]["shown"] == "Not checked yet"
    r = client.post(f"/api/containers/{cid}/check").json()
    assert r["status"] == "unhealthy" and r["shown"] == "Having problems" and r["what_to_do"] == "Check the database container."
    ov = client.get("/api/overview").json()["containers"][0]
    assert ov["status"] == "unhealthy" and ov["summary"] == "It can't reach its database."
    h = client.get(f"/api/containers/{cid}/checks").json()["checks"]
    assert len(h) == 1 and h[0]["evidence"]["prompt"].startswith("Container: web") and h[0]["model"] == "llama3.1:8b"
    assert client.post("/api/containers/9999/check").status_code == 404
    assert client.get("/api/checks/round").json()["running"] is False


def test_rule_leads_when_the_ai_only_saw_calm_logs(data_dir, docker, fake):
    setup_db([], with_ai=True)
    fake.servers["thor"].generate_handler = verdict()
    docker.add("job", "exited", 137); docker.log("job", "INFO working")
    r = first_round(docker)
    assert r["job"]["status"] == "critical" and r["job"]["summary"] == "It stopped with code 137 (SIGKILL)."


def test_a_user_stop_stays_a_stop_after_docker_forgets_the_event(data_dir, docker):
    setup_db([], with_ai=False)
    docker.add("svc", "exited", 137); docker.events_by_id["id-svc"] = ["kill", "stop", "die"]
    assert first_round(docker)["svc"]["status"] == "stopped"
    docker.events_by_id["id-svc"] = []  # flushed out of Docker's short event log
    run()
    assert state("svc")["status"] == "stopped"
    docker.containers["svc"]["State"]["FinishedAt"] = "2026-11-24T05:00:00Z"  # a new exit: judged afresh
    run()
    assert state("svc")["status"] == "critical"


def test_event_listener_memory():
    d = machines.LocalDocker("/nonexistent.sock")
    d._seen["abc"] = __import__("collections").deque([(100.0, "kill"), (101.0, "stop"), (500.0, "die")])
    assert d.remembered_events("abc", 90, 200) == ["kill", "stop"]
    assert d.events("abc", 90, 200) == ["kill", "stop"]  # no Docker call needed
