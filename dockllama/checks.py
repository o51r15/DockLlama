"""The check engine (plan section 6): rounds over the watched containers.

A round has two halves:
1. Collect, for every watched container at once: Docker facts, new log lines (redacted),
   CPU/RAM. Rules run on the facts. No AI involved, so this works without Ollama.
2. Ask the AI, one container at a time, holding the shared AI lock for the round (model tests
   wait for the round; a round waits for at most one model's test). A container is only sent
   to the AI if something changed: new log lines or different Docker facts, or 6 hours passed.

The final status is the worse of the AI's verdict and the rules'. When the AI can't answer the
status is "Couldn't check" with the reason, never "healthy" (v1 failed open to healthy).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field

from starlette.concurrency import run_in_threadpool

from . import advanced, ai_queue, ai_servers, analyzer, db, machines, prompts, rules, settings, webcheck
from .evaluate import RETRY_SUFFIX, SCHEMA, SYSTEM_PROMPT, AnswerError, parse_answer
from .ollama import OllamaClient, OllamaError, build_generate_request, truncation_warnings
from .redact import redact

log = logging.getLogger("dockllama.checks")

MAX_LINE_CHARS = 2000
FULL_CHECK_EVERY = 6 * 3600  # ask the AI at least this often even when nothing changed
COLLECT_AT_ONCE = 6
SYSTEM_PROMPT_TOKENS = 1300  # measured: the built-in evaluate prompt is ~1.2K tokens
CHARS_PER_TOKEN = 3.5
DEFAULT_AI_TIMEOUT = 120

SHOWN = {"healthy": "Working fine", "degraded": "Needs a look", "unhealthy": "Having problems",
         "critical": "Down or failing", "stopped": "Stopped", "unknown": "Couldn't check", "gone": "No longer exists"}


@dataclass
class Collected:
    row: dict
    facts: rules.Facts | None = None
    previous: rules.Facts | None = None
    rule: rules.RuleResult | None = None
    lines: list[str] = field(default_factory=list)
    last_log_ts: float | None = None
    cpu: float | None = None
    mem: float | None = None
    error: str | None = None
    gone: bool = False
    web: tuple[bool, str] | None = None  # (answering, detail) when a web page check ran


@dataclass
class RoundState:
    running: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    total: int = 0
    done: int = 0
    current: str | None = None
    phase: str = ""  # collecting | asking AI
    note: str = ""


ROUND = RoundState()


# --- collecting -----------------------------------------------------------------------------


def _facts_from_inspect(name: str, a: dict) -> rules.Facts:
    st = a.get("State") or {}
    health = st.get("Health") or {}
    hlog = health.get("Log") or []
    return rules.Facts(
        name=name, state=st.get("Status", "unknown"), exit_code=st.get("ExitCode"),
        oom_killed=bool(st.get("OOMKilled")), restart_count=int(a.get("RestartCount") or 0),
        restart_policy=((a.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no",
        health=health.get("Status"), health_streak=int(health.get("FailingStreak") or 0),
        health_output=(hlog[-1].get("Output") or "") if hlog else "",
        started_at=machines.parse_docker_time(st.get("StartedAt")),
        finished_at=machines.parse_docker_time(st.get("FinishedAt")),
    )


def _facts_from_json(text: str | None) -> rules.Facts | None:
    if not text:
        return None
    try:
        return rules.Facts(**json.loads(text))
    except (TypeError, ValueError):
        return None


async def collect(docker: machines.LocalDocker, row: dict, state: dict | None, log_lines: int) -> Collected:
    c = Collected(row=row)
    name = row["name"]
    try:
        a = await docker.ainspect(name)
    except machines.MachineError as exc:
        if "doesn't exist" in str(exc):
            c.gone = True
        else:
            c.error = str(exc)
        return c
    f = _facts_from_inspect(name, a)
    c.previous = _facts_from_json(state["facts"]) if state else None
    # Was it the user? docker stop / docker restart leave a "stop" or "restart" event; a crash or
    # docker kill doesn't. Only looked up when it matters.
    if f.state in ("exited", "dead") and f.exit_code not in (0, None) and f.finished_at:
        f.events = await docker.aevents(a["Id"], f.finished_at - 120, f.finished_at + 10)
    elif c.previous and f.restart_count > c.previous.restart_count:
        since = (state or {}).get("last_checked") or (f.started_at or time.time()) - 600
        f.events = await docker.aevents(a["Id"], since - 5, time.time())
    f.stopped_by_user = any(e in ("stop", "restart") for e in f.events)
    # Still the same exit as last time? Then it's still whatever it was judged to be then, even if
    # Docker has since forgotten the event.
    if (not f.stopped_by_user and c.previous and c.previous.stopped_by_user and f.state in ("exited", "dead")
            and c.previous.finished_at == f.finished_at):
        f.stopped_by_user = True
    if f.state == "running" and row.get("web_url"):
        ok, detail = await webcheck.check(row["web_url"], row.get("web_word") or "")
        f.web_fails = 0 if ok else (row.get("web_fails") or 0) + 1
        f.web_error = "" if ok else detail
        c.web = (ok, detail)
    c.facts = f
    c.rule = rules.evaluate(f, c.previous)
    log_driver = ((a.get("HostConfig") or {}).get("LogConfig") or {}).get("Type") or "json-file"
    if log_driver in machines.READABLE_LOG_DRIVERS and c.rule.ask_ai:
        try:
            new = await docker.alogs(name, row.get("last_log_ts"), log_lines)
        except machines.MachineError:
            new = []
        c.lines = [redact(text[:MAX_LINE_CHARS]) for _, text in new]
        c.last_log_ts = new[-1][0] if new else row.get("last_log_ts")
    else:
        c.last_log_ts = row.get("last_log_ts")
    if f.state == "running":
        c.cpu, c.mem = await docker.astats(name)
    return c


# --- asking the AI ----------------------------------------------------------------------------


def _ago(ts: float | None) -> str:
    if not ts:
        return "unknown"
    s = max(0, time.time() - ts)
    if s < 120:
        return f"{int(s)} seconds"
    if s < 7200:
        return f"{int(s // 60)} minutes"
    if s < 172800:
        return f"{int(s // 3600)} hours"
    return f"{int(s // 86400)} days"


def facts_line(f: rules.Facts) -> str:
    bits = [f.state]
    if f.state == "running":
        bits.append(f"up {_ago(f.started_at)}")
    if f.restart_count:
        bits.append(f"{f.restart_count} restarts")
    bits.append(f"health check: {f.health}" if f.health else "no health check")
    if f.state != "running" and f.exit_code is not None:
        bits.append(f"exit code {f.exit_code}")
    if f.oom_killed:
        bits.append("last stop was an out-of-memory kill")
    return ", ".join(bits)


def build_prompt(c: Collected, notes: str, interval_minutes: int, num_ctx: int,
                 system: str = SYSTEM_PROMPT) -> tuple[str, analyzer.LogSummary]:
    """The container's summary, trimmed to fit ~75% of the context window (plan 8.2)."""
    row = c.row
    summary = analyzer.analyze(c.lines, json.loads(row.get("ignore_patterns") or "[]"),
                               json.loads(row.get("known_patterns") or "[]"))
    resources = (f"CPU {c.cpu if c.cpu is not None else 'N/A'}%"
                 f" | RAM {c.mem if c.mem is not None else 'N/A'}%") if c.facts.state == "running" else "N/A"
    window = f"{interval_minutes} minutes" if row.get("last_log_ts") else "the most recent log lines"
    system_tokens = max(SYSTEM_PROMPT_TOKENS, int(len(system) / CHARS_PER_TOKEN))  # custom prompts can be longer
    budget_chars = int((num_ctx * 0.75 - system_tokens) * CHARS_PER_TOKEN)
    for tail, patterns in ((25, 10), (15, 8), (10, 5), (5, 3), (3, 2)):
        text = analyzer.build_prompt(row["name"], summary, facts_line=facts_line(c.facts), resources=resources,
                                     window=window, notes=notes, tail_lines=tail, max_patterns=patterns)
        if len(text) <= budget_chars:
            return text, summary
    return text[:max(budget_chars, 1000)], summary


async def ask_ai(client: OllamaClient, model, clean: dict, prompt: str, interval_minutes: int,
                 system: str = SYSTEM_PROMPT) -> dict:
    """One verdict. Returns {verdict | error, model, duration_s, prompt_tokens, done_reason, warnings}."""
    req = advanced.to_request(clean, interval_minutes)
    payload = build_generate_request(model, prompt, system=system, schema=SCHEMA, settings=req)
    timeout = clean.get("timeout") or DEFAULT_AI_TIMEOUT
    started = time.monotonic()
    out = {"model": model.name}
    try:
        resp = await client.generate(payload, timeout=timeout)
        try:
            verdict = parse_answer(resp.get("response", ""))
        except AnswerError:
            if not clean.get("retry", True):
                raise
            again = dict(payload, prompt=prompt + RETRY_SUFFIX)
            again["options"] = dict(payload["options"], temperature=0.0)
            resp = await client.generate(again, timeout=timeout)
            verdict = parse_answer(resp.get("response", ""))
        out.update(verdict=verdict, prompt_tokens=resp.get("prompt_eval_count"), done_reason=resp.get("done_reason"),
                   warnings=truncation_warnings(payload, resp, len(system) + len(prompt)))
    except OllamaError as exc:
        out["error"] = str(exc)
    except AnswerError as exc:
        out["error"] = f"The AI's answer couldn't be read: {exc}"
    out["duration_s"] = round(time.monotonic() - started, 2)
    return out


# --- deciding and saving ----------------------------------------------------------------------


def _save(conn, c: Collected, status: str, source: str, summary: str, what_to_do: str, ai: dict | None,
          prompt: str | None, unchanged: bool) -> None:
    now = time.time()
    cid = c.row["id"]
    state = conn.execute("SELECT * FROM container_state WHERE container_id = ?", (cid,)).fetchone()
    facts_json = json.dumps(asdict(c.facts)) if c.facts else None
    conn.execute("UPDATE containers SET last_log_ts = ? WHERE id = ?", (c.last_log_ts, cid))
    if c.web is not None:
        ok, detail = c.web
        conn.execute("UPDATE containers SET web_fails = ?, web_last_ok = CASE WHEN ? THEN ? ELSE web_last_ok END, "
                     "web_last_error = ? WHERE id = ?",
                     (c.facts.web_fails, ok, now, None if ok else detail, cid))
    if c.cpu is not None or c.mem is not None:
        conn.execute("INSERT INTO stats (container_id, at, cpu, mem) VALUES (?, ?, ?, ?)", (cid, now, c.cpu, c.mem))
    health_failing = bool(c.rule and c.rule.rule == "health_failing")
    if unchanged and state is not None:
        conn.execute("UPDATE container_state SET last_checked = ?, facts = ?, health_fail_checks = "
                     "CASE WHEN ? THEN health_fail_checks + 1 ELSE 0 END WHERE container_id = ?",
                     (now, facts_json, health_failing, cid))
        return
    verdict = (ai or {}).get("verdict") or {}
    # Full evidence when there's something to explain; a "still fine" check keeps only the basics,
    # so a chatty healthy container doesn't fill the disk with prompts.
    interesting = status not in ("healthy", "stopped") or state is None or state["status"] != status
    evidence = {"facts": asdict(c.facts) if c.facts else None, "rule": c.rule.reason if c.rule else "",
                "ai_error": (ai or {}).get("error"), "tail": c.lines[-25:] if interesting else c.lines[-3:]}
    if ai and ai.get("instructions"):
        evidence["instructions"] = ai["instructions"]  # which AI instructions judged it
    if interesting:
        evidence["prompt"] = prompt
    check_id = conn.execute(
        """INSERT INTO checks (container_id, at, status, source, summary, what_to_do, rule, health_score,
               restart_would_help, recommended_action, model, duration_s, prompt_tokens, done_reason, warnings, evidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, now, status, source, summary, what_to_do, c.rule.rule if c.rule else None, verdict.get("health_score"),
         None if not verdict else int(verdict["restart_would_help"]), verdict.get("recommended_action"),
         (ai or {}).get("model"), (ai or {}).get("duration_s"), (ai or {}).get("prompt_tokens"),
         (ai or {}).get("done_reason"), json.dumps((ai or {}).get("warnings") or []), json.dumps(evidence))).lastrowid
    streak = ((state["unknown_streak"] if state else 0) + 1) if status == "unknown" else 0
    asked_ai = source == "ai" and ai is not None and "verdict" in ai
    if state is None:
        conn.execute("""INSERT INTO container_state (container_id, status, since, summary, what_to_do, last_check_id,
                            last_checked, last_ai_check, facts, unknown_streak, health_fail_checks)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (cid, status, now, summary, what_to_do, check_id, now, now if asked_ai else None, facts_json,
                      streak, int(health_failing)))
        conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'status_change', ?)",
                     (now, cid, json.dumps({"from": None, "to": status, "summary": summary, "check_id": check_id})))
        return
    changed = state["status"] != status
    conn.execute("""UPDATE container_state SET status = ?, since = ?, summary = ?, what_to_do = ?, last_check_id = ?,
                        last_checked = ?, last_ai_check = COALESCE(?, last_ai_check), facts = ?, unknown_streak = ?,
                        health_fail_checks = CASE WHEN ? THEN health_fail_checks + 1 ELSE 0 END
                    WHERE container_id = ?""",
                 (status, now if changed else state["since"], summary, what_to_do, check_id, now,
                  now if asked_ai else None, facts_json, streak, health_failing, cid))
    if changed:
        conn.execute("INSERT INTO events (at, container_id, kind, data) VALUES (?, ?, 'status_change', ?)",
                     (now, cid, json.dumps({"from": state["status"], "to": status, "summary": summary,
                                            "check_id": check_id})))


def _no_ai_summary(c: Collected, why: str) -> tuple[str, str, str]:
    """Status, summary, what to do when only the rules decide."""
    if c.rule.status is not None:
        return c.rule.status, c.rule.reason, c.rule.what_to_do
    return "healthy", f"Running. {why}", ""


async def decide(c: Collected, ai_ctx: dict | None, force_ai: bool, state: dict | None,
                 interval_minutes: int) -> tuple:
    """Returns (status, source, summary, what_to_do, ai, prompt, unchanged)."""
    if c.gone:
        return "gone", "rules", "This container doesn't exist any more.", "Remove it from DockLlama, or recreate it.", None, None, False
    if c.facts is None:
        return "unknown", "rules", f"Couldn't check: {c.error}", "", None, None, False
    if not c.rule.ask_ai:
        return c.rule.status, "rules", c.rule.reason, c.rule.what_to_do, None, None, False
    if ai_ctx is None:
        status, summary, todo = _no_ai_summary(c, "No AI is connected, so DockLlama checked its state, restarts and health check only.")
        return status, "no_ai", summary, todo, None, None, False
    if ai_ctx.get("paused"):
        status, summary, todo = _no_ai_summary(c, "AI checks are paused while models are being tested.")
        return status, "no_ai", summary, todo, None, None, False
    # Skip the AI if nothing changed (plan 6.4).
    unchanged = (
        not force_ai and state is not None and state.get("status") not in (None, "unknown", "gone")
        and c.previous is not None and c.previous.key() == c.facts.key() and not c.lines
        and state.get("last_ai_check") and time.time() - state["last_ai_check"] < FULL_CHECK_EVERY
    )
    if unchanged:
        return state["status"], "ai", state["summary"], state["what_to_do"], None, None, True
    model, clean = ai_ctx["model_for"](c.row)
    system = ai_ctx.get("system", SYSTEM_PROMPT)
    prompt, _ = build_prompt(c, c.row.get("notes_for_ai") or "", interval_minutes,
                             clean.get("num_ctx") or 8192, system)
    ai = await ask_ai(ai_ctx["client"], model, clean, prompt, interval_minutes, system)
    ai["instructions"] = ai_ctx.get("prompt_label", "Default")
    if "error" in ai:
        status = rules.worse(c.rule.status or "unknown", "unknown")
        summary = f"Couldn't check: {ai['error']}"
        if c.rule.status:
            summary = f"{c.rule.reason} (The AI couldn't check it: {ai['error']})"
        return status, "ai", summary, c.rule.what_to_do, ai, prompt, False
    v = ai["verdict"]
    status = rules.worse(v["status"], c.rule.status or "healthy")
    summary, todo = v["summary"], v["what_to_do"]
    if c.rule.status and rules.SEVERITY[c.rule.status] > rules.SEVERITY.get(v["status"], 0):
        # The rules saw something worse than the AI did (it read logs, not Docker's state). Lead with
        # the rule; keep the AI's words only if it saw a problem too, since "working normally" would
        # contradict it.
        summary = c.rule.reason if v["status"] == "healthy" else f"{c.rule.reason} {v['summary']}"
        todo = c.rule.what_to_do or v["what_to_do"]
    if todo.strip().rstrip(".").lower() in ("nothing", "none", "no action needed", "n/a"):
        todo = ""
    return status, "ai", summary, todo, ai, prompt, False


# --- rounds -------------------------------------------------------------------------------------


def _load(container_ids: list[int] | None):
    with db.session() as conn:
        q = "SELECT * FROM containers WHERE gone = 0 AND monitored = 1"
        args: tuple = ()
        if container_ids is not None:
            q = f"SELECT * FROM containers WHERE id IN ({','.join('?' * len(container_ids)) or 'NULL'})"
            args = tuple(container_ids)
        rows = [dict(r) for r in conn.execute(q + " ORDER BY name", args)]
        states = {r["container_id"]: dict(r) for r in conn.execute(
            "SELECT s.*, k.restart_would_help AS _last_restart_would_help FROM container_state s "
            "LEFT JOIN checks k ON k.id = s.last_check_id")}
        server = ai_servers.connected(conn)
        interval = settings.get(conn, "checks.interval_minutes")
        log_lines = settings.get(conn, "checks.log_lines")
        prompt_label, system = prompts.active_system(conn)
        server = dict(server) if server else None
        models = {}
        if server:
            for m in conn.execute("SELECT * FROM models WHERE server_id = ? AND installed = 1", (server["id"],)):
                models[m["name"]] = ai_servers.model_from_row(m)
            ms = {r["name"]: json.loads(r["settings"]) for r in
                  conn.execute("SELECT name, settings FROM model_settings WHERE server_id = ?", (server["id"],))}
        else:
            ms = {}
    return rows, states, server, interval, log_lines, models, ms, (prompt_label, system)


def _ai_context(server: dict | None, models: dict, model_settings: dict,
                active_prompt: tuple[str, str] = ("Default", SYSTEM_PROMPT)) -> dict | None:
    if not server or not server.get("model") or server["model"] not in models:
        return None
    default = models[server["model"]]
    if not default.usable:
        return None

    def model_for(row: dict):
        m = models.get(row.get("model_override") or "") or default
        if not m.usable:
            m = default
        return m, model_settings.get(m.name, {})
    return {"client": OllamaClient(server["address"]), "model_for": model_for, "paused": ai_queue.checks_paused,
            "prompt_label": active_prompt[0], "system": active_prompt[1]}


async def run_checks(container_ids: list[int] | None = None, force_ai: bool = False, track: bool = True) -> list[dict]:
    """Check the watched containers (or just the given ones). Returns what each one came to."""
    rows, states, server, interval, log_lines, models, model_settings, active_prompt = await run_in_threadpool(
        _load, container_ids)
    if track:
        ROUND.__dict__.update(running=True, started_at=time.time(), finished_at=None, total=len(rows), done=0,
                              current=None, phase="collecting", note="")
    results = []
    try:
        # Keep each machine's container list in step with Docker (new and removed containers).
        machine_ids = {r["machine_id"] for r in rows}
        if container_ids is None:
            def enabled():
                with db.session() as conn:
                    return {r["id"] for r in conn.execute("SELECT id FROM machines WHERE enabled = 1")}
            machine_ids |= await run_in_threadpool(enabled)
        machine_ids = sorted(machine_ids)
        dockers, down = {}, {}
        for mid in machine_ids:
            try:
                docker = machines.get_machine(mid)
                if isinstance(docker, machines.RemoteDocker):
                    info = await docker.ainfo()
                    allows = bool(info.get("allows_restart"))

                    def note_restarts(mid=mid, allowed=allows):
                        with db.session() as conn:
                            conn.execute("UPDATE machines SET allows_restart = ? WHERE id = ?", (int(allowed), mid))
                    await run_in_threadpool(note_restarts)
                infos = await docker.alist_containers()

                def sync(mid=mid, infos=infos):
                    with db.session() as conn, db.transaction(conn):
                        machines.sync_containers(conn, mid, infos)
                await run_in_threadpool(sync)
                dockers[mid] = docker
            except machines.MachineError as exc:
                down[mid] = str(exc)
                if track and mid == 1:
                    ROUND.note = str(exc)

                def fail(mid=mid, msg=str(exc)):
                    with db.session() as conn:
                        machines.record_machine_error(conn, mid, msg)
                await run_in_threadpool(fail)
        if not dockers and container_ids is None:
            return []
        sem = asyncio.Semaphore(COLLECT_AT_ONCE)

        async def one(row):
            if row["machine_id"] in down:
                return Collected(row=row, error=down[row["machine_id"]])
            async with sem:
                return await collect(dockers[row["machine_id"]], row, states.get(row["id"]), log_lines)
        collected = await asyncio.gather(*(one(r) for r in rows))
        ai_ctx = _ai_context(server, models, model_settings, active_prompt)
        if track:
            ROUND.phase = "asking AI" if ai_ctx else "checking"
        kind = "round" if track else "check"
        single = rows[0]["id"] if (not track and len(rows) == 1) else None
        async with ai_queue.turn(kind, container_id=single):
            if ai_ctx is not None:
                ai_ctx["paused"] = ai_queue.checks_paused
            for c in collected:
                if track:
                    ROUND.current = c.row["name"]
                state = states.get(c.row["id"])
                decision = await decide(c, ai_ctx, force_ai, state, interval)
                status, source, summary, todo, ai, prompt, unchanged = decision
                if (not unchanged and ai is None and state is not None and state["status"] == status
                        and state["summary"] == summary and not force_ai):
                    unchanged = True  # rules-only verdict that hasn't changed: no new history row

                def save(c=c, verdict=(status, source, summary, todo, ai, prompt, unchanged)):
                    with db.session() as conn, db.transaction(conn):
                        _save(conn, c, *verdict)
                await run_in_threadpool(save)
                verdict = (ai or {}).get("verdict") or {}
                if unchanged and state is not None and ai is None:
                    verdict = {"restart_would_help": state.get("_last_restart_would_help")}
                results.append({"id": c.row["id"], "name": c.row["name"], "status": status, "source": source,
                                "summary": summary, "what_to_do": todo, "unchanged": unchanged,
                                "machine_id": c.row["machine_id"], "rule": c.rule.rule if c.rule else None,
                                "docker_state": c.facts.state if c.facts else None,
                                "restart_would_help": bool(verdict.get("restart_would_help"))})
                if track:
                    ROUND.done += 1
    finally:
        if track:
            ROUND.__dict__.update(running=False, finished_at=time.time(), current=None, phase="")
            if results:  # remember how long a full round takes here, for the "how often" guard
                took = ROUND.finished_at - ROUND.started_at

                def remember(took=took):
                    with db.session() as conn:
                        settings.set(conn, "checks.last_round_s", round(took, 1))
                await run_in_threadpool(remember)
        from .notify import NOTIFIER
        NOTIFIER.wake()  # send what this round changed now, not at the next minute
    return results


def prune(conn) -> dict[str, int]:
    """Delete history older than the retention setting (Settings → Advanced). The check each
    container's current status points at, and messages not sent yet, are always kept."""
    cutoff = time.time() - settings.get(conn, "data.retention_days") * 86400
    gone = {
        "checks": conn.execute("DELETE FROM checks WHERE at < ? AND id NOT IN (SELECT last_check_id FROM "
                               "container_state WHERE last_check_id IS NOT NULL)", (cutoff,)).rowcount,
        "stats": conn.execute("DELETE FROM stats WHERE at < ?", (cutoff,)).rowcount,
        "events": conn.execute("DELETE FROM events WHERE at < ? AND NOT (kind = 'status_change' AND handled = 0)",
                               (cutoff,)).rowcount,
    }
    if any(gone.values()):
        log.info("Removed history older than the retention setting: %s", gone)
    return gone


class Scheduler:
    """Runs a round every `checks.interval_minutes`, once setup is finished."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._last_prune = 0.0
        self.next_at: float | None = None

    async def start(self, first_delay: float = 20) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(first_delay), name="check-rounds")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def reschedule(self, interval_minutes: int) -> None:
        """A new interval takes effect now, not after the old wait."""
        if self._wake is not None and self.next_at is not None:
            self.next_at = min(self.next_at, time.time() + interval_minutes * 60)
            self._wake.set()

    def run_now(self) -> None:
        if self._wake is not None:
            self.next_at = time.time()
            self._wake.set()

    async def _loop(self, first_delay: float) -> None:
        self.next_at = time.time() + first_delay
        while True:
            try:
                wait = max(0.0, self.next_at - time.time())
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except TimeoutError:
                    pass
                if time.time() < self.next_at - 0.5:
                    continue  # woken for another reason
                ready, interval = await run_in_threadpool(self._settings)
                self.next_at = time.time() + interval * 60
                if ready and not ROUND.running:
                    results = await run_checks()
                    from .restarts import after_round
                    await after_round(results, run_checks)
                    if time.time() - self._last_prune > 86400:
                        def do_prune():
                            with db.session() as conn, db.transaction(conn):
                                prune(conn)
                        await run_in_threadpool(do_prune)
                        self._last_prune = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # the scheduler must never die
                log.exception("Check round failed; trying again next round")
                self.next_at = time.time() + 60

    @staticmethod
    def _settings() -> tuple[bool, int]:
        with db.session() as conn:
            return settings.get(conn, "setup.complete"), settings.get(conn, "checks.interval_minutes")


SCHEDULER = Scheduler()
