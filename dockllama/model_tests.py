"""Testing models on the user's own Ollama server (plan section 8A).

One test = the 8 scenarios on one model, back to back, so the model loads once. A test records
its score (0–100), seconds per check, load time, whether it fit on the GPU, and whether any
answer was cut off. Results are kept per server and per model version (digest), forever.

Rules that protect the user's system:
- one test at a time; tests wait for each other and never run alongside a check round;
- a model that's far too slow here stops early ("Too slow on this system");
- a scenario that times out once is retried at the end (busy-server noise);
- the tested model is unloaded afterwards, unless it was already loaded before the test
  (another app is using it) or it's the model DockLlama uses for checks.

The runner is a single background task. Runs and tests live in the database, so a test run
carries on after DockLlama restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool

from . import advanced, ai_queue, ai_servers, catalog, db, settings
from .evaluate import RETRY_SUFFIX, SCHEMA, SYSTEM_PROMPT, AnswerError, parse_answer
from .ollama import ModelDetails, OllamaClient, OllamaError, build_generate_request
from .scenarios import SCENARIOS, score_answer

log = logging.getLogger("dockllama.tests")

DEFAULT_TIMEOUT = 120  # seconds per scenario, like v1 and ollama_bench3
MIN_TOKENS_PER_SECOND = 2.0
TEST_KEEP_ALIVE = "10m"  # stays loaded across the 8 scenarios; unloaded afterwards
BUSY = ("running", "paused")  # a run in one of these greys out every other test button
FINISHED = ("done", "too_slow", "failed")


class TooSlow(Exception):
    pass


# --- one model ----------------------------------------------------------------------------


@dataclass
class Progress:
    test_id: int
    name: str
    scenario: int = 0  # scenarios finished
    phase: str = "loading"  # loading | testing | retrying | finishing


def _seconds(ns) -> float | None:
    return ns / 1e9 if isinstance(ns, (int, float)) and ns > 0 else None


async def run_scenario(client: OllamaClient, model: ModelDetails, request_settings, scenario: dict,
                       timeout: float, retry: bool = True) -> dict:
    """Ask one scenario. Never raises for a bad answer; raises OllamaError for server problems
    (kind 'timeout' when the model took too long)."""
    payload = build_generate_request(model, scenario["summary"], system=SYSTEM_PROMPT, schema=SCHEMA,
                                     settings=request_settings)
    started = time.monotonic()
    responses = [await client.generate(payload, timeout=timeout)]
    retried = False
    problem = ""
    try:
        verdict = parse_answer(responses[0].get("response", ""))
    except AnswerError as exc:
        verdict, problem = None, str(exc)
    if verdict is None and retry:  # v1 behaviour: one more try, stricter
        retried = True
        again = dict(payload, prompt=scenario["summary"] + RETRY_SUFFIX)
        again["options"] = dict(payload["options"], temperature=0.0)
        responses.append(await client.generate(again, timeout=timeout))
        try:
            verdict = parse_answer(responses[-1].get("response", ""))
        except AnswerError as exc:
            problem = str(exc)
    wall = time.monotonic() - started
    first = responses[0]
    eval_count = sum(r.get("eval_count") or 0 for r in responses)
    eval_s = sum(_seconds(r.get("eval_duration")) or 0 for r in responses)
    load_s = _seconds(first.get("load_duration"))
    total_s = sum(_seconds(r.get("total_duration")) or 0 for r in responses)
    answer_s = (total_s - (load_s or 0)) if total_s else wall - (load_s or 0)
    result = {
        "id": scenario["id"], "name": scenario["name"], "retried": retried,
        "wall_s": round(wall, 2), "answer_s": round(max(answer_s, 0.0), 2),
        "load_s": round(load_s, 2) if load_s else None,
        "tokens_per_s": round(eval_count / eval_s, 1) if eval_s > 0 and eval_count else None,
        "cut_off": any(r.get("done_reason") == "length" for r in responses),
    }
    if verdict is None:
        result.update(score=0.0, notes=[f"Couldn't read the answer: {problem}"], verdict=None)
        return result
    points, notes = score_answer(scenario, verdict)
    score = points / 100
    if retried:
        score *= 0.9
        notes.insert(0, "needed a second try to answer in the right format")
    result.update(score=round(score, 3), notes=notes, verdict={
        k: verdict[k] for k in ("status", "health_score", "recommended_action", "restart_would_help", "summary")
    })
    return result


def _fit(ps_models: list[dict], name: str) -> str:
    for m in ps_models:
        if m.get("name") == name or m.get("model") == name:
            size, vram = m.get("size") or 0, m.get("size_vram")
            if not size or vram is None:
                return "unknown"
            if vram >= size * 0.99:
                return "gpu"
            return "partial" if vram > 0 else "cpu"
    return "unknown"


async def test_model(client: OllamaClient, model: ModelDetails, clean_settings: dict, progress: Progress,
                     keep_loaded: bool) -> dict:
    """Run the 8 scenarios on one model. Returns the result fields for model_tests."""
    req = advanced.to_request(clean_settings)
    req.keep_alive = TEST_KEEP_ALIVE
    timeout = clean_settings.get("timeout") or DEFAULT_TIMEOUT
    retry = clean_settings.get("retry", True)
    results: list[dict | None] = [None] * len(SCENARIOS)
    timed_out: list[int] = []
    fit = "unknown"
    try:
        for i, scenario in enumerate(SCENARIOS):
            progress.phase = "loading" if i == 0 else "testing"
            progress.scenario = i
            try:
                r = await run_scenario(client, model, req, scenario, timeout, retry)
            except OllamaError as exc:
                if exc.kind != "timeout":
                    raise
                if i == 0:
                    raise TooSlow(f"It took longer than {timeout} seconds to answer the first question.") from exc
                timed_out.append(i)
                continue
            if i == 0 and r["tokens_per_s"] is not None and r["tokens_per_s"] < MIN_TOKENS_PER_SECOND:
                raise TooSlow(f"It wrote only {r['tokens_per_s']} tokens a second here "
                              f"(DockLlama needs at least {MIN_TOKENS_PER_SECOND:.0f}).")
            results[i] = r
            if i == 0:
                try:  # where it loaded; checked while it's surely loaded
                    fit = _fit(await client.ps(), model.name)
                except OllamaError:
                    pass
        for i in timed_out:  # busy-server noise: one more try at the end
            progress.phase, progress.scenario = "retrying", i
            try:
                results[i] = await run_scenario(client, model, req, SCENARIOS[i], timeout, retry)
            except OllamaError as exc:
                if exc.kind != "timeout":
                    raise
                results[i] = {"id": SCENARIOS[i]["id"], "name": SCENARIOS[i]["name"], "score": 0.0,
                              "notes": [f"took longer than {timeout} seconds, twice"], "verdict": None,
                              "wall_s": timeout, "answer_s": timeout, "load_s": None, "tokens_per_s": None,
                              "cut_off": False, "retried": False}
        progress.phase = "finishing"
    finally:
        if not keep_loaded:
            await client.unload(model.name)
    done = [r for r in results if r is not None]
    answered = [r for r in done if r.get("verdict") is not None]
    times = [r["answer_s"] for r in answered] or [r["answer_s"] for r in done]
    return {
        "status": "done",
        "score": round(100 * sum(r["score"] for r in done) / len(SCENARIOS), 1),
        "sec_per_check": round(sum(times) / len(times), 2) if times else None,
        "load_s": done[0]["load_s"] if done else None,
        "fit": fit,
        "cut_off": int(any(r["cut_off"] for r in done)),
        "details": done,
    }


# --- choosing between results -------------------------------------------------------------

FIT_ORDER = {"gpu": 0, "unknown": 1, "partial": 2, "cpu": 3}
MIN_SUGGEST_SCORE = 70  # below this a model isn't worth suggesting, whatever it's compared with


def baseline_for(name: str | None, results: dict[str, dict]) -> dict | None:
    """What a candidate is compared with: the current model's test here, or, if it was never
    tested here, its catalog score (from DockLlama's own runs)."""
    if not name:
        return None
    if name in results and not results[name].get("older_version"):
        return results[name]
    entry = catalog.catalog_entry(name)
    if entry:
        return {"status": "done", "score": entry["score"], "sec_per_check": None, "fit": "unknown", "from_catalog": True}
    return None


def meaningfully_better(candidate: dict, current: dict | None) -> bool:
    """Plan 8A: ≥3 points better at similar speed, or about the same score (within 2) and
    ≥30% faster; never by moving from the GPU to partly-CPU."""
    if candidate.get("status") != "done" or candidate.get("score") is None:
        return False
    if candidate["score"] < MIN_SUGGEST_SCORE:
        return False
    if current is None or current.get("status") != "done" or current.get("score") is None:
        return True  # the current model failed its test, was too slow, or was never tested
    if FIT_ORDER.get(candidate.get("fit"), 1) > FIT_ORDER.get(current.get("fit"), 1) and current.get("fit") == "gpu":
        return False
    c_sec, cur_sec = candidate.get("sec_per_check"), current.get("sec_per_check")
    similar_speed = c_sec is None or cur_sec is None or c_sec <= cur_sec * 1.3
    if candidate["score"] >= current["score"] + 3 and similar_speed:
        return True
    if abs(candidate["score"] - current["score"]) <= 2 and c_sec is not None and cur_sec and c_sec <= cur_sec * 0.7:
        return True
    return False


def best_result(results: dict[str, dict]) -> dict | None:
    """The best tested model: highest score; among those within 2 points of it, prefer one
    that fits on the GPU, then the fastest."""
    done = [r for r in results.values() if r.get("status") == "done" and r.get("score") is not None
            and r["score"] >= MIN_SUGGEST_SCORE and not r.get("older_version")]
    if not done:
        return None
    top = max(r["score"] for r in done)
    close = [r for r in done if r["score"] >= top - 2]
    return min(close, key=lambda r: (FIT_ORDER.get(r.get("fit"), 1), r.get("sec_per_check") or 999, -r["score"]))


def describe(result: dict) -> str:
    bits = [f"scored {result['score']:.0f}/100"]
    if result.get("sec_per_check"):
        bits.append(f"{result['sec_per_check']:.1f} s per check")
    where = {"gpu": "fits on the graphics card", "partial": "runs partly on the CPU", "cpu": "runs on the CPU"}
    if result.get("fit") in where:
        bits.append(where[result["fit"]])
    return ", ".join(bits)


# --- the database side --------------------------------------------------------------------


def _row(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def result_dict(row: sqlite3.Row, current_digest: str | None = None) -> dict:
    d = {k: row[k] for k in ("id", "run_id", "name", "digest", "status", "score", "sec_per_check", "load_s",
                             "fit", "error", "started_at", "finished_at")}
    d["cut_off"] = bool(row["cut_off"])
    d["settings"] = json.loads(row["settings"])
    d["older_version"] = bool(current_digest and row["digest"] and row["digest"] != current_digest)
    return d


def latest_results(conn: sqlite3.Connection, server_id: int) -> dict[str, dict]:
    """The newest finished test of each installed model on this server."""
    digests = {r["name"]: r["digest"] for r in conn.execute(
        "SELECT name, digest FROM models WHERE server_id = ? AND installed = 1", (server_id,))}
    rows = conn.execute(
        f"""SELECT * FROM model_tests WHERE server_id = ? AND status IN ({",".join("?" * len(FINISHED))})
            ORDER BY finished_at""", (server_id, *FINISHED)).fetchall()
    out = {}
    for row in rows:
        if row["name"] in digests:
            out[row["name"]] = result_dict(row, digests[row["name"]])
    return out


def history(conn: sqlite3.Connection, server_id: int, name: str) -> list[dict]:
    digest = _row(conn, "SELECT digest FROM models WHERE server_id = ? AND name = ?", server_id, name)
    rows = conn.execute(
        f"""SELECT * FROM model_tests WHERE server_id = ? AND name = ? AND status IN ({",".join("?" * len(FINISHED))})
            ORDER BY finished_at DESC LIMIT 20""", (server_id, name, *FINISHED)).fetchall()
    out = []
    for row in rows:
        d = result_dict(row, digest["digest"] if digest else None)
        d["details"] = json.loads(row["details"])
        out.append(d)
    return out


def busy_run(conn: sqlite3.Connection):
    return conn.execute(f"SELECT * FROM test_runs WHERE status IN ({','.join('?' * len(BUSY))}) ORDER BY id",
                        BUSY).fetchone()


def tonight(conn: sqlite3.Connection, now: float | None = None) -> float:
    """The next start of quiet hours, as a timestamp."""
    tz = ZoneInfo(settings.get(conn, "general.timezone"))
    hh, mm = (int(x) for x in settings.get(conn, "quiet.start").split(":"))
    local_now = datetime.fromtimestamp(now or time.time(), tz)
    start = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if start <= local_now:
        start += timedelta(days=1)
    return start.timestamp()


def create_run(conn: sqlite3.Connection, server_id: int, names: list[str], kind: str, mode: str,
               start_after: float | None = None) -> int:
    """Caller holds a transaction and has checked that nothing else is running."""
    baseline = _row(conn, "SELECT model FROM ai_servers WHERE id = ?", server_id)["model"]
    status = "scheduled" if start_after else ("waiting" if kind == "new" else "running")
    run_id = conn.execute(
        "INSERT INTO test_runs (server_id, kind, mode, status, start_after, baseline, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (server_id, kind, mode, status, start_after, baseline, time.time())).lastrowid
    for pos, name in enumerate(names):
        conn.execute("INSERT INTO model_tests (run_id, server_id, name, position, status) VALUES (?, ?, ?, ?, 'queued')",
                     (run_id, server_id, name, pos))
    return run_id


def queue_new_models(conn: sqlite3.Connection, server_id: int, names: list[str]) -> int | None:
    """Plan 8A "new models later": queue a background test of newly installed models, unless
    the user turned that off or they're already queued. Caller holds a transaction."""
    if not names or not settings.get(conn, "ai.auto_test_new"):
        return None
    usable = []
    for name in names:
        row = _row(conn, "SELECT * FROM models WHERE server_id = ? AND name = ? AND installed = 1", server_id, name)
        if row is not None and ai_servers.model_from_row(row).usable:
            queued = _row(conn, "SELECT 1 FROM model_tests t JOIN test_runs r ON r.id = t.run_id WHERE "
                                "t.server_id = ? AND t.name = ? AND t.status IN ('queued', 'running') "
                                "AND r.status IN ('waiting', 'scheduled', 'running', 'paused')", server_id, name)
            if queued is None:
                usable.append(name)
    if not usable:
        return None
    waiting = _row(conn, "SELECT id FROM test_runs WHERE server_id = ? AND kind = 'new' AND status = 'waiting'", server_id)
    if waiting is None:
        return create_run(conn, server_id, usable, "new", "background")
    pos = _row(conn, "SELECT COALESCE(MAX(position), -1) + 1 p FROM model_tests WHERE run_id = ?", waiting["id"])["p"]
    for i, name in enumerate(usable):
        conn.execute("INSERT INTO model_tests (run_id, server_id, name, position, status) VALUES (?, ?, ?, ?, 'queued')",
                     (waiting["id"], server_id, name, pos + i))
    return waiting["id"]


def estimate_seconds(model: ModelDetails, hardware: str, past: dict | None = None) -> float:
    """Rough time for one model's test, for the "about 40 minutes" estimate."""
    if past and past.get("status") == "done" and past.get("sec_per_check"):
        return (past.get("load_s") or 5) + len(SCENARIOS) * past["sec_per_check"] + 5
    gb = model.size / 1e9
    seconds = 5 + 1.5 * gb + len(SCENARIOS) * (1.5 + 0.35 * gb)
    if model.thinking == "levels":
        seconds *= 1.8  # models that can't stop thinking write more
    if not catalog.fits(model.size / catalog.GIB, hardware):
        seconds *= 3
    return seconds


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Mark a run done and decide whether the "better model" banner should show."""
    run = _row(conn, "SELECT * FROM test_runs WHERE id = ?", run_id)
    banner = "none"
    any_done = _row(conn, "SELECT 1 FROM model_tests WHERE run_id = ? AND status = 'done'", run_id) is not None
    if run["kind"] in ("all", "new") and any_done:
        server = _row(conn, "SELECT * FROM ai_servers WHERE id = ?", run["server_id"])
        results = latest_results(conn, run["server_id"])
        best = best_result(results)
        better = (best is not None and best["name"] != server["model"]
                  and meaningfully_better(best, baseline_for(server["model"], results)))
        if better or run["kind"] == "all":
            banner = "pending"
    conn.execute("UPDATE test_runs SET status = 'done', finished_at = ?, banner = ? WHERE id = ?",
                 (time.time(), banner, run_id))


def banner(conn: sqlite3.Connection) -> dict | None:
    """What the "model testing finished" banner should say, if anything (plan 3.2 / 8A)."""
    server = ai_servers.connected(conn)
    if server is None:
        return None
    run = _row(conn, "SELECT * FROM test_runs WHERE server_id = ? AND banner = 'pending' ORDER BY id DESC LIMIT 1",
               server["id"])
    if run is None:
        return None
    results = latest_results(conn, server["id"])
    best = best_result(results)
    current = baseline_for(server["model"], results)
    if best is not None and best["name"] != server["model"] and meaningfully_better(best, current):
        text = f"Model testing finished. {best['name']} {describe(best)}"
        if current and current.get("from_catalog"):
            text += f"; your current model {server['model']} hasn't been tested on this server."
        elif current and current.get("status") == "done":
            text += f"; your current model {server['model']} {describe(current)}."
        elif server["model"]:
            text += f"; your current model {server['model']} didn't finish its test."
        else:
            text += "."
        return {"run_id": run["id"], "type": "better", "name": best["name"], "text": text}
    if run["kind"] == "all":
        if not server["model"]:
            text = "Model testing finished. See the results in Settings → AI."
        elif server["model"] in results:
            text = "Model testing finished. Your current model is the best fit for this server."
        else:
            text = "Model testing finished. None of the tested models did clearly better than your current model."
        return {"run_id": run["id"], "type": "best", "name": server["model"], "text": text}
    conn.execute("UPDATE test_runs SET banner = 'dismissed' WHERE id = ?", (run["id"],))
    return None


def recover(conn: sqlite3.Connection) -> None:
    """After a restart: tests that were mid-way start again; watched runs carry on in the background."""
    conn.execute("UPDATE model_tests SET status = 'queued', scenario = 0, started_at = NULL WHERE status = 'running'")
    conn.execute("UPDATE test_runs SET mode = 'background' WHERE mode = 'watch' AND status IN ('running', 'paused')")


# --- the runner ---------------------------------------------------------------------------


class Runner:
    """The single background task that works through queued tests."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._current: asyncio.Task | None = None
        self._stop_reason: str | None = None  # "pause" | "cancel" when the user interrupts
        self.progress: Progress | None = None
        self.current_run: int | None = None

    async def start(self) -> None:
        def _recover():
            with db.session() as conn, db.transaction(conn):
                recover(conn)
        await run_in_threadpool(_recover)
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="model-tests")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        ai_queue.checks_paused = False

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def interrupt(self, run_id: int, reason: str) -> None:
        """Stop the test in progress, if it belongs to run_id (pause or cancel)."""
        if self._current is not None and self.current_run == run_id and not self._current.done():
            self._stop_reason = reason
            self._current.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                job = await run_in_threadpool(self._next_job)
                if job is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._run(*job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the runner must never die
                log.exception("Model test runner hit an error; carrying on")
                await asyncio.sleep(5)

    @staticmethod
    def _next_job():
        with db.session() as conn, db.transaction(conn):
            server = ai_servers.connected(conn)
            while True:
                run = _row(conn, "SELECT * FROM test_runs WHERE status = 'running' ORDER BY id")
                if run is None:
                    if _row(conn, "SELECT 1 FROM test_runs WHERE status = 'paused'"):
                        return None
                    run = _row(conn, "SELECT * FROM test_runs WHERE status = 'scheduled' AND start_after <= ? "
                                     "ORDER BY start_after", time.time()) \
                        or _row(conn, "SELECT * FROM test_runs WHERE status = 'waiting' ORDER BY id")
                    if run is None:
                        return None
                    conn.execute("UPDATE test_runs SET status = 'running' WHERE id = ?", (run["id"],))
                if server is None or run["server_id"] != server["id"]:
                    conn.execute("UPDATE model_tests SET status = 'cancelled' WHERE run_id = ? AND status = 'queued'",
                                 (run["id"],))
                    conn.execute("UPDATE test_runs SET status = 'cancelled', finished_at = ?, note = ? WHERE id = ?",
                                 (time.time(), "DockLlama was switched to a different AI server.", run["id"]))
                    continue
                test = _row(conn, "SELECT * FROM model_tests WHERE run_id = ? AND status = 'queued' "
                                  "ORDER BY position LIMIT 1", run["id"])
                if test is None:
                    finish_run(conn, run["id"])
                    continue
                model_row = _row(conn, "SELECT * FROM models WHERE server_id = ? AND name = ? AND installed = 1",
                                 server["id"], test["name"])
                if model_row is None or not ai_servers.model_from_row(model_row).usable:
                    conn.execute("UPDATE model_tests SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                                 ("This model isn't installed any more, or can't be used.", time.time(), test["id"]))
                    continue
                s = _row(conn, "SELECT settings FROM model_settings WHERE server_id = ? AND name = ?",
                         server["id"], test["name"])
                clean = json.loads(s["settings"]) if s else {}
                model = ai_servers.model_from_row(model_row)
                conn.execute("UPDATE model_tests SET status = 'running', started_at = ?, digest = ?, settings = ?, "
                             "scenario = 0 WHERE id = ?", (time.time(), model.digest, json.dumps(clean), test["id"]))
                return dict(run), dict(test), model, clean, dict(server)

    async def _run(self, run: dict, test: dict, model: ModelDetails, clean: dict, server: dict) -> None:
        client = OllamaClient(server["address"])
        self.progress = Progress(test["id"], model.name)
        self.current_run = run["id"]
        self._stop_reason = None
        outcome: dict
        async with ai_queue.lock():
            ai_queue.checks_paused = run["mode"] == "watch"
            try:
                loaded_before = {m.get("name") for m in await client.ps()}
            except OllamaError:
                loaded_before = set()
            keep = model.name in loaded_before or model.name == server["model"]
            self._current = asyncio.create_task(test_model(client, model, clean, self.progress, keep))
            try:
                outcome = await self._current
            except asyncio.CancelledError:
                if self._stop_reason is None:
                    raise  # DockLlama is shutting down; recover() requeues the test
                outcome = {"status": "queued" if self._stop_reason == "pause" else "cancelled"}
            except TooSlow as exc:
                outcome = {"status": "too_slow", "error": f"Too slow on this system. {exc}"}
            except OllamaError as exc:
                outcome = {"status": "failed", "error": str(exc)}
                if exc.kind == "unreachable":
                    outcome = {"status": "queued", "pause_note": f"Paused: the AI server stopped answering. {exc}"}
            finally:
                self._current = None
                ai_queue.checks_paused = False
        self.progress = None
        self.current_run = None
        await run_in_threadpool(self._save, run["id"], test["id"], outcome)

    @staticmethod
    def _save(run_id: int, test_id: int, outcome: dict) -> None:
        with db.session() as conn, db.transaction(conn):
            status = outcome["status"]
            if status == "queued":
                conn.execute("UPDATE model_tests SET status = 'queued', started_at = NULL, scenario = 0 WHERE id = ?",
                             (test_id,))
                if outcome.get("pause_note"):
                    conn.execute("UPDATE test_runs SET status = 'paused', note = ? WHERE id = ?",
                                 (outcome["pause_note"], run_id))
                return
            conn.execute(
                """UPDATE model_tests SET status = ?, score = ?, sec_per_check = ?, load_s = ?, fit = ?,
                       cut_off = ?, details = ?, error = ?, finished_at = ?, scenario = ? WHERE id = ?""",
                (status, outcome.get("score"), outcome.get("sec_per_check"), outcome.get("load_s"),
                 outcome.get("fit"), outcome.get("cut_off", 0), json.dumps(outcome.get("details", [])),
                 outcome.get("error"), time.time(), len(SCENARIOS) if status == "done" else 0, test_id))


RUNNER = Runner()
