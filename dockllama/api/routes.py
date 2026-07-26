"""FastAPI routes for the DockLlama web interface."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from dockllama.config import DockLlamaConfig, ContainerConfig, save_poll_interval, save_default_model, save_containers_to_config
from dockllama.db import init_db, save_benchmark_result, get_benchmark_results, archive_container_config, restore_container_config, purge_container_data, get_container_prompt, save_container_prompt, delete_container_prompt, get_tested_models, save_tested_model, get_health_check, get_all_health_checks, save_health_check, delete_health_check, get_health_check_history, save_health_check_result
from dockllama.docker_client import get_client, get_logs, list_containers
from dockllama.log_pipeline import process_logs
from dockllama.ai_engine import evaluate, EvaluationContext
from dockllama.health_checker import get_hc_status, get_all_hc_statuses
from dockllama.db import get_all_blackout_windows, get_blackout_window, save_blackout_window, delete_blackout_window, is_blackout_active

router = APIRouter(prefix="/api")

# Set by main.py at startup
_cfg: DockLlamaConfig | None = None


def set_config(cfg: DockLlamaConfig) -> None:
    global _cfg
    _cfg = cfg


def _get_cfg() -> DockLlamaConfig:
    if _cfg is None:
        raise HTTPException(500, "Config not initialized")
    return _cfg


# --- Models ---

class ContainerStatus(BaseModel):
    name: str
    running: bool
    image: str
    status: str
    last_evaluation: Optional[dict] = None
    health_check: Optional[dict] = None


class PromptConfig(BaseModel):
    context_prompt: Optional[str] = None
    examples: Optional[list[dict]] = None
    known_patterns: Optional[list[dict]] = None


class EvalRequest(BaseModel):
    lines: Optional[list[str]] = None
    tail: int = 200


class EventRecord(BaseModel):
    id: int
    container: str
    timestamp: str
    event_type: str
    ai_status: Optional[str]
    confidence: Optional[int]
    root_cause_category: Optional[str]
    summary: Optional[str]
    action_taken: Optional[str]
    model_used: Optional[str]


# --- Endpoints ---

# ── Cached Docker container snapshot (avoids blocking dashboard on Docker daemon) ──
_container_snapshot: dict = {"data": None, "ts": 0.0}
_SNAPSHOT_TTL = 8.0  # seconds


async def _get_container_snapshot() -> dict:
    """Return {name: {image, status, running}} for all containers.

    Cached for a few seconds and fetched in a worker thread so the dashboard
    never blocks the event loop or competes with the eval loop's Docker calls.
    Reads image name from already-loaded attrs (no per-container image inspect).
    """
    now = time.monotonic()
    snap = _container_snapshot["data"]
    if snap is not None and (now - _container_snapshot["ts"]) < _SNAPSHOT_TTL:
        return snap

    client = get_client()

    def _fetch() -> dict:
        result = {}
        for c in client.containers.list(all=True):
            attrs = c.attrs or {}
            image = attrs.get("Config", {}).get("Image", "untagged")
            status = c.status
            result[c.name] = {
                "image": image,
                "status": status,
                "running": status == "running",
            }
        return result

    data = await asyncio.to_thread(_fetch)
    _container_snapshot["data"] = data
    _container_snapshot["ts"] = now
    return data


import time as _time

# Trends cache — 5 min TTL
_trends_cache = None
_trends_cache_time = 0.0
_TRENDS_TTL = 300

@router.get("/containers")
async def get_containers() -> list[ContainerStatus]:
    """List all monitored containers with their latest evaluation."""
    cfg = _get_cfg()
    snapshot = await _get_container_snapshot()
    conn = init_db(cfg.monitoring.db_path)

    result = []

    for container_cfg in cfg.containers:
        if not container_cfg.enabled:
            continue

        info = snapshot.get(container_cfg.name)
        image = info["image"] if info else ""
        container_status = info["status"] if info else "not found"
        is_running = info["running"] if info else False

        # Get latest evaluation
        row = conn.execute(
            """SELECT ai_status, confidence, root_cause_category, summary,
                      action_taken, timestamp, model_used, health_score
               FROM events WHERE container = ? AND event_type IN ('evaluation', 'on_demand_eval')
               ORDER BY id DESC LIMIT 1""",
            (container_cfg.name,),
        ).fetchone()

        last_eval = None
        if row:
            last_eval = {
                "ai_status": row[0],
                "confidence": row[1],
                "root_cause_category": row[2],
                "summary": row[3],
                "action_taken": row[4],
                "timestamp": row[5],
                "model_used": row[6],
                "health_score": row[7],
            }

        # Health check status
        hc_st = get_hc_status(container_cfg.name)
        hc_data = None
        if hc_st.get("status") != "unknown":
            hc_data = {
                "status": hc_st.get("status"),
                "consecutive_failures": hc_st.get("consecutive_failures", 0),
                "last_status_code": hc_st.get("last_status_code"),
                "last_response_ms": hc_st.get("last_response_ms"),
                "last_error": hc_st.get("last_error"),
            }

        result.append(ContainerStatus(
            name=container_cfg.name,
            running=is_running,
            image=image,
            status=container_status,
            last_evaluation=last_eval,
            health_check=hc_data,
        ))

    conn.close()
    return result



@router.get("/containers/{name}/logs")
async def get_container_logs(name: str, tail: int = Query(200, ge=1, le=2000)):
    """Fetch recent logs from a container, with pre-filtering applied."""
    cfg = _get_cfg()
    client = get_client()
    matches = [c for c in client.containers.list() if c.name == name]
    if not matches:
        raise HTTPException(404, f"Container '{name}' not running")

    container = matches[0]
    raw = get_logs(container, tail=tail)

    # Find ignore patterns from config
    ignore_patterns = []
    for c in cfg.containers:
        if c.name == name:
            ignore_patterns = c.ignore_patterns
            break

    batch = process_logs(name, raw, ignore_patterns=ignore_patterns, max_lines=tail)

    return {
        "container": name,
        "total_lines": batch.total_lines,
        "filtered_count": len(batch.filtered_lines),
        "dropped_ignore": batch.dropped_by_ignore,
        "dropped_level": batch.dropped_by_level,
        "lines": batch.filtered_lines,
        "raw_lines": raw.strip().splitlines()[-tail:],
    }


@router.post("/containers/{name}/evaluate")
async def evaluate_container(name: str, req: EvalRequest):
    """Trigger an on-demand AI evaluation of a container's logs."""
    cfg = _get_cfg()
    client = get_client()
    matches = [c for c in client.containers.list() if c.name == name]
    if not matches:
        raise HTTPException(404, f"Container '{name}' not running")

    container = matches[0]

    if req.lines:
        filtered_lines = req.lines
    else:
        raw = get_logs(container, tail=req.tail)
        ignore_patterns = []
        for c in cfg.containers:
            if c.name == name:
                ignore_patterns = c.ignore_patterns
                break
        batch = process_logs(name, raw, ignore_patterns=ignore_patterns, max_lines=req.tail)
        filtered_lines = batch.filtered_lines

    model = cfg.ollama.default_model
    for c in cfg.containers:
        if c.name == name and c.model_override:
            model = c.model_override
            break

    conn = init_db(cfg.monitoring.db_path)
    baseline = None
    row = conn.execute(
        "SELECT healthy_log_sample FROM baselines WHERE container = ?", (name,)
    ).fetchone()
    if row:
        baseline = row[0]
    conn.close()

    ctx = EvaluationContext(
        container_name=name,
        filtered_lines=filtered_lines,
        model=model,
        baseline_sample=baseline,
    )

    start = time.time()
    result, prompt_version = await evaluate(ctx, cfg.ollama)
    elapsed = round(time.time() - start, 2)

    # Save to events DB so dashboard reflects the new evaluation
    conn = init_db(cfg.monitoring.db_path)
    try:
        log_snapshot = "\n".join(filtered_lines[-50:]) if filtered_lines else ""
        conn.execute(
            "INSERT INTO events (container, event_type, ai_status, confidence, "
            "root_cause_category, summary, action_taken, log_snapshot, model_used, health_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                "on_demand_eval",
                result.status,
                result.confidence,
                result.root_cause_category,
                result.summary,
                result.recommended_action,
                log_snapshot,
                model,
                result.health_score,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "container": name,
        "status": result.status,
        "health_score": result.health_score,
        "confidence": result.confidence,
        "root_cause_category": result.root_cause_category,
        "summary": result.summary,
        "recommended_action": result.recommended_action,
        "model": model,
        "prompt_version": prompt_version,
        "eval_time_seconds": elapsed,
        "lines_evaluated": len(filtered_lines),
    }


@router.get("/events")
async def get_events(
    container: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[EventRecord]:
    """Paginated event history with optional filters."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)

    query = "SELECT id, container, timestamp, event_type, ai_status, confidence, root_cause_category, summary, action_taken, model_used FROM events WHERE 1=1"
    params: list = []

    if container:
        query += " AND container = ?"
        params.append(container)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [
        EventRecord(
            id=r[0], container=r[1], timestamp=r[2], event_type=r[3],
            ai_status=r[4], confidence=r[5], root_cause_category=r[6],
            summary=r[7], action_taken=r[8], model_used=r[9],
        )
        for r in rows
    ]


@router.get("/events/{container}/restarts")
async def get_restart_history(container: str, limit: int = Query(20, ge=1, le=100)):
    """Restart history with stored log snapshots and AI reasoning."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)

    rows = conn.execute(
        """SELECT id, timestamp, ai_status, confidence, root_cause_category,
                  summary, action_taken, log_snapshot, model_used
           FROM events WHERE container = ? AND action_taken IN ('restart', 'dry_run_restart')
           ORDER BY id DESC LIMIT ?""",
        (container, limit),
    ).fetchall()
    conn.close()

    return [
        {
            "id": r[0], "timestamp": r[1], "ai_status": r[2], "confidence": r[3],
            "root_cause_category": r[4], "summary": r[5], "action_taken": r[6],
            "log_snapshot": r[7], "model_used": r[8],
        }
        for r in rows
    ]


@router.post("/digest")
async def trigger_digest():
    """Trigger an on-demand daily digest."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        from dockllama.digest import send_digest
        digest = await send_digest(cfg, conn)
        return digest
    except Exception as e:
        raise HTTPException(500, f"Digest generation failed: {e}")
    finally:
        conn.close()



@router.get("/trends")
async def get_trends(container: Optional[str] = None):
    """7-day and 30-day health trends per container (cached 5min)."""
    global _trends_cache, _trends_cache_time
    now = _time.time()
    cache_key = container or "__fleet__"
    if _trends_cache is not None and (now - _trends_cache_time) < _TRENDS_TTL:
        if isinstance(_trends_cache, dict) and _trends_cache.get("_key") == cache_key:
            result = dict(_trends_cache)
            result.pop("_key", None)
            return result
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        from dockllama.trends import get_fleet_trends, get_container_trends
        if container:
            result = get_container_trends(conn, container)
        else:
            names = [c.name for c in cfg.containers if c.enabled]
            result = get_fleet_trends(conn, names)
        _trends_cache = dict(result) if isinstance(result, dict) else result
        if isinstance(_trends_cache, dict):
            _trends_cache["_key"] = cache_key
        _trends_cache_time = now
        return result
    except Exception as e:
        raise HTTPException(500, f"Trend calculation failed: {e}")
    finally:
        conn.close()


@router.get("/config")
async def get_config():
    """Current running configuration (sanitized)."""
    cfg = _get_cfg()
    return {
        "ollama": {
            "base_url": cfg.ollama.base_url,
            "default_model": cfg.ollama.default_model,
            "digest_model": cfg.ollama.digest_model,
            "timeout_seconds": cfg.ollama.timeout_seconds,
        },
        "monitoring": {
            "poll_interval_seconds": cfg.monitoring.poll_interval_seconds,
            "log_lines_per_check": cfg.monitoring.log_lines_per_check,
            "dry_run": cfg.monitoring.dry_run,
        },
        "containers": [
            {"name": c.name, "enabled": c.enabled, "model_override": c.model_override,
             "compose_group": c.compose_group}
            for c in cfg.containers
        ],
        "cooldowns": {
            "initial_minutes": cfg.cooldowns.initial_minutes,
            "backoff_multiplier": cfg.cooldowns.backoff_multiplier,
            "max_cooldown_minutes": cfg.cooldowns.max_cooldown_minutes,
            "max_restarts_per_hour": cfg.cooldowns.max_restarts_per_hour,
        },
        "digest": {
            "enabled": cfg.digest.enabled,
            "schedule_cron": cfg.digest.schedule_cron,
        },
    }


@router.get("/health")
async def health_check():
    """System health: Docker, Ollama, DB."""
    cfg = _get_cfg()
    checks = {"docker": False, "ollama": False, "database": False}

    # Docker
    try:
        client = get_client()
        client.ping()
        checks["docker"] = True
    except Exception:
        pass

    # Ollama
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{cfg.ollama.base_url}/api/tags")
            checks["ollama"] = r.status_code == 200
    except Exception:
        pass

    # Database
    try:
        conn = init_db(cfg.monitoring.db_path)
        conn.execute("SELECT 1")
        checks["database"] = True
        conn.close()
    except Exception:
        pass

    all_ok = all(checks.values())
    return {"healthy": all_ok, "checks": checks}



# --- Container Prompt Management ---

@router.get("/containers/{name}/prompt")
async def get_prompt(name: str):
    """Get prompt configuration for a container (DB override + config.yaml fallback)."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        # Check DB first
        db_prompt = get_container_prompt(conn, name)

        # Get config.yaml values as fallback
        config_vals = None
        for c in cfg.containers:
            if c.name == name:
                config_vals = {
                    "context_prompt": c.context_prompt,
                    "examples": c.examples,
                    "known_patterns": c.known_patterns,
                }
                break

        if not config_vals and not db_prompt:
            raise HTTPException(404, f"Container '{name}' not found in config")

        # Merge: DB wins over config
        effective = {
            "container": name,
            "context_prompt": None,
            "examples": [],
            "known_patterns": [],
            "source": "config",
            "updated_at": None,
        }
        if config_vals:
            effective["context_prompt"] = config_vals["context_prompt"]
            effective["examples"] = config_vals["examples"]
            effective["known_patterns"] = config_vals["known_patterns"]
        if db_prompt:
            effective["context_prompt"] = db_prompt["context_prompt"]
            effective["examples"] = db_prompt["examples"]
            effective["known_patterns"] = db_prompt["known_patterns"]
            effective["source"] = "database"
            effective["updated_at"] = db_prompt["updated_at"]

        # Also return raw config values for reference
        effective["config_fallback"] = config_vals
        return effective
    finally:
        conn.close()


@router.put("/containers/{name}/prompt")
async def update_prompt(name: str, prompt: PromptConfig):
    """Save prompt overrides to database (overrides config.yaml)."""
    cfg = _get_cfg()
    # Verify container exists in config
    if not any(c.name == name for c in cfg.containers):
        raise HTTPException(404, f"Container '{name}' not found in config")

    conn = init_db(cfg.monitoring.db_path)
    try:
        save_container_prompt(
            conn, name,
            context_prompt=prompt.context_prompt,
            examples=prompt.examples,
            known_patterns=prompt.known_patterns,
        )
        return {"status": "ok", "container": name, "source": "database"}
    finally:
        conn.close()


@router.delete("/containers/{name}/prompt")
async def delete_prompt(name: str):
    """Delete DB prompt overrides, reverting to config.yaml values."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        deleted = delete_container_prompt(conn, name)
        if not deleted:
            raise HTTPException(404, f"No database overrides for '{name}'")
        return {"status": "ok", "container": name, "reverted_to": "config"}
    finally:
        conn.close()


@router.post("/containers/{name}/test-prompt")
async def test_prompt(name: str, prompt: PromptConfig):
    """Test a prompt configuration against the container's current logs without saving."""
    cfg = _get_cfg()
    client = get_client()
    matches = [c for c in client.containers.list() if c.name == name]
    if not matches:
        raise HTTPException(404, f"Container '{name}' not running")

    container = matches[0]
    raw = get_logs(container, tail=cfg.monitoring.log_lines_per_check)

    # Find container config
    container_cfg = None
    for c in cfg.containers:
        if c.name == name:
            container_cfg = c
            break
    if not container_cfg:
        raise HTTPException(404, f"Container '{name}' not in config")

    ignore_patterns = container_cfg.ignore_patterns
    batch = process_logs(name, raw, ignore_patterns=ignore_patterns, max_lines=cfg.monitoring.log_lines_per_check)

    model = container_cfg.model_override or cfg.ollama.default_model

    # Build context with the TEST prompt values
    ctx = EvaluationContext(
        container_name=name,
        filtered_lines=batch.filtered_lines,
        model=model,
        context_prompt=prompt.context_prompt,
        examples=prompt.examples or [],
    )

    start = time.time()
    result, prompt_version = await evaluate(ctx, cfg.ollama)
    elapsed = round(time.time() - start, 2)

    return {
        "container": name,
        "status": result.status,
        "health_score": result.health_score,
        "confidence": result.confidence,
        "summary": result.summary,
        "model": model,
        "eval_time_seconds": elapsed,
        "lines_evaluated": len(batch.filtered_lines),
        "test_mode": True,
    }




# --- Model Management ---

@router.get("/models")
async def list_models():
    """List all models available in Ollama, with test status from DB."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        # Get tested models from DB
        tested = {m["model"]: m for m in get_tested_models(conn)}

        # Query Ollama for available models
        import httpx
        models = []
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(f"{cfg.ollama.base_url}/api/tags")
                if r.status_code == 200:
                    data = r.json()
                    for m in data.get("models", []):
                        name = m.get("name", "")
                        size_bytes = m.get("size", 0)
                        size_gb = round(size_bytes / (1024**3), 1) if size_bytes else 0
                        test_info = tested.get(name, {})
                        models.append({
                            "name": name,
                            "size_gb": size_gb,
                            "parameter_size": m.get("details", {}).get("parameter_size", ""),
                            "quantization": m.get("details", {}).get("quantization_level", ""),
                            "family": m.get("details", {}).get("family", ""),
                            "modified_at": m.get("modified_at", ""),
                            "is_current": name == cfg.ollama.default_model,
                            "is_digest": name == cfg.ollama.digest_model,
                            "test_status": test_info.get("status", "untested"),
                            "test_info": test_info if test_info else None,
                        })
        except Exception as e:
            raise HTTPException(502, f"Failed to reach Ollama: {e}")

        return {
            "models": models,
            "current_default": cfg.ollama.default_model,
            "current_digest": cfg.ollama.digest_model,
        }
    finally:
        conn.close()


class ModelTestRequest(BaseModel):
    model: str


@router.post("/models/test")
async def test_model(req: ModelTestRequest):
    """Run validation tests against a model (healthy fixture + failing fixture)."""
    cfg = _get_cfg()

    # Hardcoded test fixtures
    healthy_summary = """Container: test-healthy-fixture
Time Window: 15 minutes
Resource Usage: CPU 2.1% | RAM 18.4%
Log Severities: 0 ERROR, 0 WARN, 12 INFO
Recent Tail (last 10 lines):
[1/10] LOG: checkpoint starting: time
[2/10] LOG: checkpoint complete: wrote 42 buffers (0.3%)
[3/10] LOG: automatic analyze of table "public.users" system usage is 1%
[4/10] LOG: checkpoint starting: time
[5/10] LOG: checkpoint complete: wrote 18 buffers (0.1%)
[6/10] LOG: connection received: host=172.19.0.3 port=54210
[7/10] LOG: connection authorized: user=app database=prod
[8/10] LOG: disconnection: session time: 0:00:02.104
[9/10] LOG: checkpoint starting: time
[10/10] LOG: checkpoint complete: wrote 5 buffers (0.0%)"""

    failing_summary = """Container: test-failing-fixture
Time Window: 15 minutes
Resource Usage: CPU 99.8% | RAM 97.2%
Log Severities: 14 ERROR, 3 WARN, 0 INFO
Deduplicated Errors:
  "Out of memory: Killed process 1842 (node)" (×6)
  "Container exceeded memory limit, OOM killed" (×4)
  "FATAL: the database system is shutting down" (×2)
  "panic: runtime error: invalid memory address or nil pointer dereference" (×2)
Recovery: No recovery detected — errors continue through most recent lines.
Restart Sequence: Detected — container restarting repeatedly.
Recent Tail (last 10 lines):
[1/10] ERROR: Out of memory: Killed process 1842 (node)
[2/10] ERROR: Container exceeded memory limit, OOM killed
[3/10] WARN: Container restart count: 5 in last 10 minutes
[4/10] ERROR: FATAL: the database system is shutting down
[5/10] ERROR: Out of memory: Killed process 1843 (node)
[6/10] ERROR: Container exceeded memory limit, OOM killed
[7/10] ERROR: panic: runtime error: invalid memory address or nil pointer dereference
[8/10] WARN: process exited with code 137 (SIGKILL)
[9/10] ERROR: Out of memory: Killed process 1844 (node)
[10/10] ERROR: Container exceeded memory limit, OOM killed"""

    from dockllama.ai_engine import evaluate, EvaluationContext

    # Warmup: load model into VRAM before timed tests
    import httpx as _httpx
    import time as _time
    try:
        async with _httpx.AsyncClient(timeout=cfg.ollama.timeout_seconds) as wc:
            await wc.post(
                f"{cfg.ollama.base_url}/api/generate",
                json={"model": req.model, "prompt": "Ready.", "stream": False,
                      "options": {"num_predict": 1}},
            )
    except Exception:
        pass  # warmup failure is non-fatal

    results = {}
    times = []


    # Test 1: Healthy fixture
    import time as _time
    ctx_healthy = EvaluationContext(
        container_name="test-healthy-fixture",
        filtered_lines=[],
        structured_summary=healthy_summary,
        model=req.model,
    )
    t0 = _time.time()
    try:
        result_h, _ = await evaluate(ctx_healthy, cfg.ollama)
        t_healthy = round((_time.time() - t0) * 1000)
        times.append(t_healthy)
        healthy_pass = result_h.health_score >= 80 and result_h.status == "healthy"
        results["healthy"] = {
            "passed": healthy_pass,
            "status": result_h.status,
            "health_score": result_h.health_score,
            "confidence": result_h.confidence,
            "summary": result_h.summary,
            "response_ms": t_healthy,
            "expected": "status=healthy, score>=80",
        }
    except Exception as e:
        results["healthy"] = {"passed": False, "error": str(e), "response_ms": 0}
        healthy_pass = False

    # Test 2: Failing fixture
    ctx_failing = EvaluationContext(
        container_name="test-failing-fixture",
        filtered_lines=[],
        structured_summary=failing_summary,
        model=req.model,
    )
    t0 = _time.time()
    try:
        result_f, _ = await evaluate(ctx_failing, cfg.ollama)
        t_failing = round((_time.time() - t0) * 1000)
        times.append(t_failing)
        failing_pass = result_f.health_score < 40 and result_f.status in ("unhealthy", "critical")
        results["failing"] = {
            "passed": failing_pass,
            "status": result_f.status,
            "health_score": result_f.health_score,
            "confidence": result_f.confidence,
            "summary": result_f.summary,
            "response_ms": t_failing,
            "expected": "status=unhealthy/critical, score<40",
        }
    except Exception as e:
        results["failing"] = {"passed": False, "error": str(e), "response_ms": 0}
        failing_pass = False

    avg_ms = round(sum(times) / len(times)) if times else 0
    all_passed = healthy_pass and failing_pass

    # Save to DB
    conn = init_db(cfg.monitoring.db_path)
    try:
        import json as _json
        results_json = _json.dumps({"results": results, "avg_response_ms": avg_ms, "passed": all_passed})
        save_tested_model(conn, req.model, healthy_pass, failing_pass, avg_ms, results_json)
    finally:
        conn.close()

    return {
        "model": req.model,
        "passed": all_passed,
        "results": results,
        "avg_response_ms": avg_ms,
        "status": "supported" if all_passed else "failed",
    }


class SetDefaultModelRequest(BaseModel):
    model: str
    role: str = "eval"  # "eval" or "digest"


@router.put("/models/default")
async def set_default_model(req: SetDefaultModelRequest):
    """Set the default model (requires model to be tested/supported for eval role)."""
    cfg = _get_cfg()

    if req.role == "eval":
        # Check if model has been tested and passed
        conn = init_db(cfg.monitoring.db_path)
        try:
            tested = {m["model"]: m for m in get_tested_models(conn)}
            test_info = tested.get(req.model)
            if not test_info or test_info["status"] != "supported":
                raise HTTPException(400, f"Model '{req.model}' must pass validation tests before it can be set as default. Run tests first.")
        finally:
            conn.close()
        cfg.ollama.default_model = req.model
        save_default_model(cfg, "eval")
        return {"status": "ok", "role": "eval", "model": req.model}
    elif req.role == "digest":
        cfg.ollama.digest_model = req.model
        save_default_model(cfg, "digest")
        return {"status": "ok", "role": "digest", "model": req.model}
    else:
        raise HTTPException(400, f"Invalid role: {req.role}")



@router.get("/models/tested/{model_name:path}")
async def get_tested_model_detail(model_name: str):
    """Get stored test results for a specific model."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        row = conn.execute(
            "SELECT model, tested_at, healthy_pass, failing_pass, avg_response_ms, status, results_json "
            "FROM tested_models WHERE model = ?", (model_name,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No test results for model '{model_name}'")
        import json as _json
        result = {
            "model": row[0], "tested_at": row[1],
            "healthy_pass": bool(row[2]), "failing_pass": bool(row[3]),
            "avg_response_ms": row[4], "status": row[5],
        }
        if row[6]:
            result["detail"] = _json.loads(row[6])
        return result
    finally:
        conn.close()

@router.get("/models/tested")
async def list_tested_models():
    """List all previously tested models."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        return {"models": get_tested_models(conn)}
    finally:
        conn.close()



# --- Interval Calculator (Phase 9.3) ---

@router.get("/models/interval-calc")
async def calculate_interval(model: str = None):
    """Calculate recommended poll interval based on model benchmark and container count."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        tested = get_tested_models(conn)
    finally:
        conn.close()

    # Find the current default model's avg_response_ms
    target_model = model or cfg.ollama.default_model
    is_default = (target_model == cfg.ollama.default_model)
    model_info = None
    for m in tested:
        if m["model"] == target_model:
            model_info = m
            break

    container_count = len([c for c in cfg.containers if c.enabled])
    avg_ms = model_info["avg_response_ms"] if model_info else 0

    if avg_ms == 0:
        # No benchmark data — return defaults with a note
        return {
            "current_model": target_model,
            "is_default": is_default,
            "avg_response_ms": 0,
            "container_count": container_count,
            "total_work_seconds": 0,
            "recommended_interval": cfg.monitoring.poll_interval_seconds,
            "current_interval": cfg.monitoring.poll_interval_seconds,
            "zones": {"red_max": 0, "yellow_max": 0, "green_start": 0},
            "note": "No benchmark data. Validate this model first." if model else "No benchmark data. Validate the current model first.",
        }

    avg_seconds = avg_ms / 1000.0
    total_work = avg_seconds * container_count
    buffer_2min = total_work + 120
    buffer_5min = total_work + 300

    return {
        "current_model": target_model,
        "is_default": is_default,
        "avg_response_ms": avg_ms,
        "container_count": container_count,
        "total_work_seconds": round(total_work),
        "recommended_interval": round(buffer_5min),
        "current_interval": cfg.monitoring.poll_interval_seconds,
        "zones": {
            "red_max": round(total_work),
            "yellow_max": round(buffer_2min),
            "green_start": round(buffer_5min),
        },
        "note": None,
    }


class IntervalUpdateRequest(BaseModel):
    poll_interval_seconds: int


@router.put("/config/interval")
async def update_interval(req: IntervalUpdateRequest):
    """Update the runtime poll interval (does not persist to config.yaml)."""
    if req.poll_interval_seconds < 60:
        raise HTTPException(400, "Interval must be at least 60 seconds")
    if req.poll_interval_seconds > 7200:
        raise HTTPException(400, "Interval must be at most 7200 seconds (2 hours)")
    cfg = _get_cfg()
    old = cfg.monitoring.poll_interval_seconds
    cfg.monitoring.poll_interval_seconds = req.poll_interval_seconds
    save_poll_interval(cfg)
    return {
        "status": "ok",
        "old_interval": old,
        "new_interval": req.poll_interval_seconds,
    }




# --- Container Management (Phase 9.4) ---

@router.get("/docker/containers")
async def list_docker_containers():
    """List all Docker containers (running and stopped) with their monitoring status."""
    cfg = _get_cfg()
    client = get_client()
    monitored = {c.name for c in cfg.containers}

    result = []
    for dc in client.containers.list(all=True):
        result.append({
            "name": dc.name,
            "image": dc.image.tags[0] if dc.image.tags else "untagged",
            "status": dc.status,
            "running": dc.status == "running",
            "monitored": dc.name in monitored,
        })

    result.sort(key=lambda x: (not x["monitored"], x["name"]))
    return result


class AddContainerRequest(BaseModel):
    name: str
    enabled: bool = True


@router.post("/containers/add")
async def add_container(req: AddContainerRequest):
    """Add a container to monitoring. Restores archived config if available."""
    cfg = _get_cfg()

    # Check if already monitored
    for c in cfg.containers:
        if c.name == req.name:
            return {"status": "already_monitored", "name": req.name}

    # Check if container exists in Docker
    client = get_client()
    matches = [c for c in client.containers.list(all=True) if c.name == req.name]
    if not matches:
        raise HTTPException(404, f"Container '{req.name}' not found in Docker")

    conn = init_db(cfg.monitoring.db_path)
    try:
        # Check for archived config
        archived = restore_container_config(conn, req.name)
        restored = False

        if archived:
            new_container = ContainerConfig(
                name=req.name,
                enabled=req.enabled,
                ignore_patterns=archived.get("ignore_patterns", []),
                compose_group=archived.get("compose_group"),
                model_override=archived.get("model_override"),
            )
            restored = True
        else:
            new_container = ContainerConfig(name=req.name, enabled=req.enabled)

        cfg.containers.append(new_container)
        save_containers_to_config(cfg)

        return {
            "status": "added",
            "name": req.name,
            "restored_config": restored,
            "archived_at": archived.get("archived_at") if archived else None,
        }
    finally:
        conn.close()


@router.delete("/containers/{container_name:path}")
async def remove_container(container_name: str, purge: bool = False):
    """Remove a container from monitoring. Optionally purge all history data."""
    cfg = _get_cfg()

    # Find container in config
    target = None
    for c in cfg.containers:
        if c.name == container_name:
            target = c
            break

    if not target:
        raise HTTPException(404, f"Container '{container_name}' is not monitored")

    conn = init_db(cfg.monitoring.db_path)
    try:
        if purge:
            # Delete everything including any archived config
            deleted = purge_container_data(conn, container_name)
        else:
            # Archive config before removing
            archive_container_config(
                conn, container_name,
                ignore_patterns=target.ignore_patterns,
                compose_group=target.compose_group,
                model_override=target.model_override,
                enabled=target.enabled,
            )
            deleted = {}

        # Remove from in-memory config
        cfg.containers = [c for c in cfg.containers if c.name != container_name]
        save_containers_to_config(cfg)

        return {
            "status": "removed",
            "name": container_name,
            "purged": purge,
            "deleted_rows": deleted,
        }
    finally:
        conn.close()


# --- Stats History (Phase 7B) ---

@router.get("/containers/{container_name:path}/stats")
async def get_container_stats_history(container_name: str, range: str = "24h"):
    """Get historical stats for a container. Range: 1h, 24h, 7d, 30d."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(range, 24)
        cutoff = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            - __import__("datetime").timedelta(hours=hours)
        ).strftime("%Y-%m-%d %H:%M:%S")

        rows = conn.execute(
            "SELECT timestamp, cpu_percent, mem_percent, mem_usage_mb, net_rx_bytes, net_tx_bytes "
            "FROM container_stats WHERE container = ? AND timestamp >= ? ORDER BY timestamp",
            (container_name, cutoff),
        ).fetchall()

        points = [
            {"t": r[0], "cpu": r[1], "mem": r[2], "mem_mb": r[3], "rx": r[4], "tx": r[5]}
            for r in rows
        ]

        # Downsample for longer ranges
        max_points = {"1h": 60, "24h": 288, "7d": 336, "30d": 360}.get(range, 288)
        if len(points) > max_points:
            step = len(points) / max_points
            downsampled = []
            i = 0.0
            while int(i) < len(points):
                idx = int(i)
                downsampled.append(points[idx])
                i += step
            points = downsampled

        return {"container": container_name, "range": range, "count": len(points), "data": points}
    finally:
        conn.close()


@router.get("/stats/fleet")
async def get_fleet_stats(range: str = "24h"):
    """Get aggregate fleet stats (total CPU/RAM across all containers)."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(range, 24)
        cutoff = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            - __import__("datetime").timedelta(hours=hours)
        ).strftime("%Y-%m-%d %H:%M:%S")

        # Group by timestamp (rounded to minute), sum CPU/RAM across containers
        rows = conn.execute(
            "SELECT strftime('%%Y-%%m-%%d %%H:%%M', timestamp) as ts, "
            "SUM(cpu_percent) as total_cpu, SUM(mem_usage_mb) as total_mem_mb, "
            "COUNT(DISTINCT container) as container_count "
            "FROM container_stats WHERE timestamp >= ? "
            "GROUP BY ts ORDER BY ts",
            (cutoff,),
        ).fetchall()

        points = [
            {"t": r[0], "cpu": round(r[1], 1) if r[1] else 0, "mem_mb": round(r[2], 1) if r[2] else 0, "containers": r[3]}
            for r in rows
        ]

        # Downsample
        max_points = {"1h": 60, "24h": 288, "7d": 336, "30d": 360}.get(range, 288)
        if len(points) > max_points:
            step = len(points) / max_points
            downsampled = []
            i = 0.0
            while int(i) < len(points):
                downsampled.append(points[int(i)])
                i += step
            points = downsampled

        # Current snapshot (latest per container)
        latest = conn.execute(
            "SELECT container, cpu_percent, mem_percent, mem_usage_mb "
            "FROM container_stats WHERE id IN ("
            "  SELECT MAX(id) FROM container_stats GROUP BY container"
            ")"
        ).fetchall()

        current = {
            "total_cpu": round(sum(r[1] or 0 for r in latest), 1),
            "total_mem_mb": round(sum(r[3] or 0 for r in latest), 1),
            "container_count": len(latest),
        }

        return {"range": range, "count": len(points), "current": current, "data": points}
    finally:
        conn.close()




# --- Health Checks (Phase 6.5) ---

class BlackoutWindowConfig(BaseModel):
    name: str
    days: list[int]  # 0=Mon, 6=Sun
    start_time: str = "00:00"
    end_time: str = "23:59"
    enabled: bool = True


class HealthCheckConfig(BaseModel):
    url: str
    method: str = "GET"
    expected_status: int = 200
    interval_seconds: int = 60
    timeout_seconds: int = 10
    failure_threshold: int = 3
    enabled: bool = False


@router.get("/containers/{name}/healthcheck")
async def get_healthcheck(name: str):
    """Get health check config for a container."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        hc = get_health_check(conn, name)
        if not hc:
            return {"container": name, "configured": False}
        hc["configured"] = True
        return hc
    finally:
        conn.close()


@router.put("/containers/{name}/healthcheck")
async def update_healthcheck(name: str, hc: HealthCheckConfig):
    """Configure health check for a container."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        save_health_check(
            conn, name, url=hc.url, method=hc.method,
            expected_status=hc.expected_status, interval_seconds=hc.interval_seconds,
            timeout_seconds=hc.timeout_seconds, failure_threshold=hc.failure_threshold,
            enabled=hc.enabled,
        )
        return {"status": "ok", "container": name}
    finally:
        conn.close()


@router.delete("/containers/{name}/healthcheck")
async def remove_healthcheck(name: str):
    """Remove health check config for a container."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        deleted = delete_health_check(conn, name)
        if not deleted:
            raise HTTPException(404, f"No health check for '{name}'")
        return {"status": "ok", "container": name}
    finally:
        conn.close()


@router.get("/containers/{name}/healthcheck/history")
async def get_healthcheck_history(name: str, limit: int = Query(20, ge=1, le=100)):
    """Get recent health check results."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        return {"container": name, "results": get_health_check_history(conn, name, limit)}
    finally:
        conn.close()


@router.get("/healthchecks")
async def list_healthchecks():
    """List all health check configs with their current status."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        checks = get_all_health_checks(conn)
        # Add latest result for each
        for hc in checks:
            history = get_health_check_history(conn, hc["container"], limit=1)
            hc["last_result"] = history[0] if history else None
        return {"checks": checks}
    finally:
        conn.close()



# --- Setup Wizard (Phase 9.5) ---

@router.get("/setup/status")
async def setup_status():
    """Check if first-run setup is needed."""
    cfg = _get_cfg()
    has_containers = len([c for c in cfg.containers if c.enabled]) > 0
    has_ollama = False
    ollama_url = cfg.ollama.base_url
    default_model = cfg.ollama.default_model

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{ollama_url}/api/tags")
            has_ollama = r.status_code == 200
    except Exception:
        pass

    has_docker = False
    try:
        client = get_client()
        client.ping()
        has_docker = True
    except Exception:
        pass

    # Check if any model has been tested
    conn = init_db(cfg.monitoring.db_path)
    try:
        tested = get_tested_models(conn)
        has_tested_model = any(m["status"] == "supported" for m in tested)
    finally:
        conn.close()

    needs_setup = not has_containers or not has_ollama or not has_tested_model
    return {
        "needs_setup": needs_setup,
        "has_containers": has_containers,
        "has_ollama": has_ollama,
        "has_docker": has_docker,
        "has_tested_model": has_tested_model,
        "ollama_url": ollama_url,
        "default_model": default_model,
        "container_count": len([c for c in cfg.containers if c.enabled]),
    }


class OllamaUrlRequest(BaseModel):
    base_url: str


@router.put("/setup/ollama-url")
async def update_ollama_url(req: OllamaUrlRequest):
    """Update the Ollama base URL and persist to config.yaml."""
    cfg = _get_cfg()
    old = cfg.ollama.base_url
    cfg.ollama.base_url = req.base_url.rstrip("/")

    # Test connectivity
    connected = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{cfg.ollama.base_url}/api/tags")
            connected = r.status_code == 200
    except Exception:
        pass

    if not connected:
        cfg.ollama.base_url = old
        raise HTTPException(400, f"Cannot connect to Ollama at {req.base_url}")

    # Persist to config.yaml
    from dockllama.config import _update_yaml_field
    _update_yaml_field("base_url", cfg.ollama.base_url)

    return {"status": "ok", "base_url": cfg.ollama.base_url, "connected": True}



# --- Alert management ---

class AlertConfig(BaseModel):
    urls: list[str]


@router.get("/alerts")
async def get_alerts():
    """Get current alert configuration (from DB)."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        from dockllama.alerts import load_alert_urls
        urls = load_alert_urls(conn)
        return {"urls": urls}
    finally:
        conn.close()


@router.put("/alerts")
async def update_alerts(alert_cfg: AlertConfig):
    """Update alert URLs (persisted to database)."""
    cfg = _get_cfg()
    cfg.alerts.urls = alert_cfg.urls
    from dockllama.alerts import init_alerts, save_alert_urls
    init_alerts(alert_cfg.urls)
    conn = init_db(cfg.monitoring.db_path)
    try:
        save_alert_urls(conn, alert_cfg.urls)
    finally:
        conn.close()
    return {"status": "ok", "count": len(alert_cfg.urls), "persisted": True}


@router.post("/alerts/test")
async def test_alerts():
    """Send a test notification to all configured alert targets."""
    cfg = _get_cfg()
    if not cfg.alerts.urls:
        raise HTTPException(400, "No alert URLs configured")
    from dockllama.alerts import _send
    import apprise
    success = _send(
        "DockLlama Test Notification",
        "This is a test notification from DockLlama. If you see this, notifications are working!",
        apprise.NotifyType.INFO,
    )
    return {"success": success, "targets": len(cfg.alerts.urls)}


# --- Digest history ---

@router.get("/digests/latest")
async def get_latest_digest():
    """Get the most recent digest."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        row = conn.execute(
            "SELECT id, date, generated_at, overall_health, headline, digest_json, formatted_text "
            "FROM digests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise HTTPException(404, "No digests generated yet")
        import json as _json
        return {
            "id": row[0], "date": row[1], "generated_at": row[2],
            "overall_health": row[3], "headline": row[4],
            "digest": _json.loads(row[5]), "formatted_text": row[6],
        }
    finally:
        conn.close()


@router.get("/digests/{date}")
async def get_digest_by_date(date: str):
    """Get digest for a specific date (YYYY-MM-DD)."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        row = conn.execute(
            "SELECT id, date, generated_at, overall_health, headline, digest_json, formatted_text "
            "FROM digests WHERE date = ? ORDER BY id DESC LIMIT 1",
            (date,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No digest for {date}")
        import json as _json
        return {
            "id": row[0], "date": row[1], "generated_at": row[2],
            "overall_health": row[3], "headline": row[4],
            "digest": _json.loads(row[5]), "formatted_text": row[6],
        }
    finally:
        conn.close()


@router.get("/digests")
async def list_digests(limit: int = Query(30, ge=1, le=365)):
    """List available digest dates."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        rows = conn.execute(
            "SELECT id, date, overall_health, headline FROM digests ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"id": r[0], "date": r[1], "overall_health": r[2], "headline": r[3]} for r in rows]
    finally:
        conn.close()



# ─── Blackout Windows ─────────────────────────────────────────────

@router.get("/blackouts")
async def list_blackouts():
    """List all blackout windows."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        windows = get_all_blackout_windows(conn)
        return {"windows": windows}
    finally:
        conn.close()


@router.get("/blackouts/active")
async def get_active_blackout():
    """Check if a blackout window is currently active."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        active = is_blackout_active(conn)
        return {"active": active is not None, "window": active}
    finally:
        conn.close()


@router.post("/blackouts")
async def create_blackout(bw: BlackoutWindowConfig):
    """Create a new blackout window."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        wid = save_blackout_window(
            conn, name=bw.name, days=bw.days,
            start_time=bw.start_time, end_time=bw.end_time, enabled=bw.enabled,
        )
        return {"status": "ok", "id": wid}
    finally:
        conn.close()


@router.put("/blackouts/{window_id}")
async def update_blackout(window_id: int, bw: BlackoutWindowConfig):
    """Update an existing blackout window."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        existing = get_blackout_window(conn, window_id)
        if not existing:
            raise HTTPException(404, f"Blackout window {window_id} not found")
        save_blackout_window(
            conn, name=bw.name, days=bw.days,
            start_time=bw.start_time, end_time=bw.end_time, enabled=bw.enabled,
            window_id=window_id,
        )
        return {"status": "ok", "id": window_id}
    finally:
        conn.close()


@router.delete("/blackouts/{window_id}")
async def remove_blackout(window_id: int):
    """Delete a blackout window."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        deleted = delete_blackout_window(conn, window_id)
        if not deleted:
            raise HTTPException(404, f"Blackout window {window_id} not found")
        return {"status": "ok"}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════
# Hidden Admin Benchmark System
# ═══════════════════════════════════════════════════

BENCHMARK_SCENARIOS = [
    {
        "id": "S1", "name": "Clean Healthy Container", "difficulty": "easy",
        "summary": (
            "Container: test-healthy-postgres\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 2.1% | RAM 18.4%\n"
            "Log Severities: 0 ERROR, 0 WARN, 12 INFO\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] LOG: checkpoint starting: time\n"
            "[2/10] LOG: checkpoint complete: wrote 42 buffers (0.3%)\n"
            "[3/10] LOG: automatic analyze of table \"public.users\" system usage is 1%\n"
            "[4/10] LOG: checkpoint starting: time\n"
            "[5/10] LOG: checkpoint complete: wrote 18 buffers (0.1%)\n"
            "[6/10] LOG: connection received: host=172.19.0.3 port=54210\n"
            "[7/10] LOG: connection authorized: user=app database=prod\n"
            "[8/10] LOG: disconnection: session time: 0:00:02.104\n"
            "[9/10] LOG: checkpoint starting: time\n"
            "[10/10] LOG: checkpoint complete: wrote 5 buffers (0.0%)"
        ),
        "expect_status": "healthy", "score_min": 85, "score_max": 100,
        "expect_action": "none", "expect_restart": False,
    },
    {
        "id": "S2", "name": "OOM Crash Loop", "difficulty": "easy",
        "summary": (
            "Container: test-failing-worker\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 99.8% | RAM 97.2%\n"
            "Log Severities: 14 ERROR, 3 WARN, 0 INFO\n"
            "Deduplicated Errors:\n"
            "  \"Out of memory: Killed process 1842 (node)\" (x6)\n"
            "  \"Container exceeded memory limit, OOM killed\" (x4)\n"
            "  \"FATAL: the database system is shutting down\" (x2)\n"
            "  \"panic: runtime error: invalid memory address\" (x2)\n"
            "Recovery: No recovery detected - errors continue through most recent lines.\n"
            "Restart Sequence: Detected - container restarting repeatedly.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] ERROR: Out of memory: Killed process 1842 (node)\n"
            "[2/10] ERROR: Container exceeded memory limit, OOM killed\n"
            "[3/10] WARN: Container restart count: 5 in last 10 minutes\n"
            "[4/10] ERROR: FATAL: the database system is shutting down\n"
            "[5/10] ERROR: Out of memory: Killed process 1843 (node)\n"
            "[6/10] ERROR: Container exceeded memory limit, OOM killed\n"
            "[7/10] ERROR: panic: runtime error: invalid memory address\n"
            "[8/10] WARN: process exited with code 137 (SIGKILL)\n"
            "[9/10] ERROR: Out of memory: Killed process 1844 (node)\n"
            "[10/10] ERROR: Container exceeded memory limit, OOM killed"
        ),
        "expect_status": "critical", "score_min": 0, "score_max": 15,
        "expect_action": "restart", "expect_restart": True,
    },
    {
        "id": "S3", "name": "Recovered After Errors", "difficulty": "medium",
        "summary": (
            "Container: test-api-gateway\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 12.3% | RAM 45.1%\n"
            "Log Severities: 5 ERROR, 2 WARN, 35 INFO\n"
            "Deduplicated Errors:\n"
            "  \"Connection refused to upstream backend:8080\" (x3, lines 2-8)\n"
            "  \"Failed health check for service payments\" (x2, lines 5-9)\n"
            "Recovery: YES - clean operation from line 15 onward, no errors in last 35 lines.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: GET /api/users 200 12ms\n"
            "[2/10] INFO: POST /api/orders 201 45ms\n"
            "[3/10] INFO: GET /api/health 200 2ms\n"
            "[4/10] INFO: GET /api/products 200 18ms\n"
            "[5/10] INFO: POST /api/payments 201 89ms\n"
            "[6/10] INFO: GET /api/users 200 11ms\n"
            "[7/10] INFO: GET /api/health 200 1ms\n"
            "[8/10] INFO: POST /api/orders 201 52ms\n"
            "[9/10] INFO: GET /api/products 200 15ms\n"
            "[10/10] INFO: GET /api/health 200 2ms"
        ),
        "expect_status": "healthy", "score_min": 80, "score_max": 100,
        "expect_action": "none", "expect_restart": False,
    },
    {
        "id": "S4", "name": "Routine Errors (Known Patterns)", "difficulty": "medium",
        "summary": (
            "Container: test-bitmagnet\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 3.5% | RAM 22.8%\n"
            "Log Severities: 18 (18 routine) ERROR, 15 (15 routine) WARN, 5 INFO\n"
            "Container-Specific Context: BitTorrent indexer. DHT errors, tracker timeouts, peer resets are normal.\n"
            "Deduplicated Errors:\n"
            "  \"dial tcp 45.33.32.156:6881: i/o timeout\" (x8) [ROUTINE: DHT network timeout]\n"
            "  \"tracker responded with 503\" (x5) [ROUTINE: tracker temporarily unavailable]\n"
            "  \"peer connection reset by remote\" (x5) [ROUTINE: peer disconnected]\n"
            "Recovery: N/A (all errors are routine)\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: indexed 142 new torrents in last 5m\n"
            "[2/10] WARN: torrent metadata fetch timed out [ROUTINE: slow metadata]\n"
            "[3/10] ERROR: dial tcp 91.121.59.153:6881: i/o timeout [ROUTINE: DHT network timeout]\n"
            "[4/10] INFO: DHT nodes: 2847 active\n"
            "[5/10] ERROR: tracker responded with 503 [ROUTINE: tracker temporarily unavailable]\n"
            "[6/10] WARN: DHT node unresponsive [ROUTINE: DHT churn]\n"
            "[7/10] INFO: indexed 138 new torrents in last 5m\n"
            "[8/10] ERROR: peer connection reset by remote [ROUTINE: peer disconnected]\n"
            "[9/10] INFO: processing queue: 45 pending\n"
            "[10/10] WARN: torrent metadata fetch timed out [ROUTINE: slow metadata]"
        ),
        "expect_status": "healthy", "score_min": 85, "score_max": 100,
        "expect_action": "none", "expect_restart": False,
    },
    {
        "id": "S5", "name": "Degraded Performance", "difficulty": "medium",
        "summary": (
            "Container: test-web-frontend\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 78.4% | RAM 71.2%\n"
            "Log Severities: 3 ERROR, 12 WARN, 40 INFO\n"
            "Deduplicated Errors:\n"
            "  \"upstream response timeout (30s)\" (x3)\n"
            "Deduplicated Warnings:\n"
            "  \"request processing time exceeded 5s\" (x8)\n"
            "  \"connection pool near capacity (45/50)\" (x4)\n"
            "Recovery: UNCLEAR - slow responses intermittent throughout window.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: GET /dashboard 200 3200ms\n"
            "[2/10] WARN: request processing time exceeded 5s\n"
            "[3/10] INFO: GET /api/data 200 1800ms\n"
            "[4/10] INFO: GET /static/app.js 200 12ms\n"
            "[5/10] WARN: connection pool near capacity (45/50)\n"
            "[6/10] INFO: POST /api/submit 201 4200ms\n"
            "[7/10] WARN: request processing time exceeded 5s\n"
            "[8/10] INFO: GET /api/status 200 890ms\n"
            "[9/10] INFO: GET /dashboard 200 2100ms\n"
            "[10/10] WARN: request processing time exceeded 5s"
        ),
        "expect_status": "degraded", "score_min": 40, "score_max": 70,
        "expect_action": "none", "expect_restart": False,
    },
    {
        "id": "S6", "name": "Graceful Shutdown (FATAL is Normal)", "difficulty": "hard",
        "summary": (
            "Container: test-postgres-replica\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 0.5% | RAM 12.3%\n"
            "Log Severities: 6 ERROR, 0 WARN, 8 INFO\n"
            "Deduplicated Errors:\n"
            "  \"FATAL: the database system is shutting down\" (x3, lines 8-10)\n"
            "  \"FATAL: terminating connection due to administrator command\" (x3, lines 8-12)\n"
            "Recovery: YES - container restarted successfully, accepting connections.\n"
            "Restart Sequence: Detected - single clean restart (not a loop).\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] LOG: received fast shutdown request\n"
            "[2/10] LOG: aborting any active transactions\n"
            "[3/10] FATAL: terminating connection due to administrator command\n"
            "[4/10] FATAL: the database system is shutting down\n"
            "[5/10] LOG: all server processes terminated; reinitializing\n"
            "[6/10] LOG: database system was shut down at 2026-07-25 22:15:03 UTC\n"
            "[7/10] LOG: database system is ready to accept connections\n"
            "[8/10] LOG: checkpoint starting: time\n"
            "[9/10] LOG: checkpoint complete: wrote 0 buffers (0.0%)\n"
            "[10/10] LOG: connection received: host=172.19.0.5 port=43210"
        ),
        "expect_status": "healthy", "score_min": 75, "score_max": 100,
        "expect_action": "none", "expect_restart": False,
    },
    {
        "id": "S7", "name": "High CPU Normal Workload", "difficulty": "hard",
        "summary": (
            "Container: test-plex-transcoder\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 95.2% | RAM 68.4%\n"
            "Log Severities: 0 ERROR, 0 WARN, 28 INFO\n"
            "Errors: NONE\n"
            "Recovery: N/A (no errors detected)\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: Transcoding session started for user mike - 4K HEVC to 1080p H264\n"
            "[2/10] INFO: Transcode speed: 2.1x realtime\n"
            "[3/10] INFO: Transcoding session started for user sarah - 4K HDR to 720p\n"
            "[4/10] INFO: Transcode speed: 1.8x realtime\n"
            "[5/10] INFO: Direct play session for user john\n"
            "[6/10] INFO: Transcode speed: 2.0x realtime\n"
            "[7/10] INFO: Media analysis complete: /data/movies/new_release.mkv\n"
            "[8/10] INFO: Transcode speed: 1.9x realtime\n"
            "[9/10] INFO: Transcoding session started for user alex - 1080p to 480p\n"
            "[10/10] INFO: Transcode speed: 4.2x realtime"
        ),
        "expect_status": "healthy", "score_min": 85, "score_max": 100,
        "expect_action": "none", "expect_restart": False,
    },
    {
        "id": "S8", "name": "External Dependency Failure", "difficulty": "hard",
        "summary": (
            "Container: test-payment-processor\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 5.3% | RAM 28.1%\n"
            "Log Severities: 10 ERROR, 8 WARN, 20 INFO\n"
            "Deduplicated Errors:\n"
            "  \"HTTP 503 from https://api.stripe.com/v1/charges\" (x5)\n"
            "  \"HTTP 502 from https://api.stripe.com/v1/refunds\" (x3)\n"
            "  \"Connection timed out: api.stripe.com:443\" (x2)\n"
            "Deduplicated Warnings:\n"
            "  \"Retry attempt 3/5 for payment charge\" (x5)\n"
            "  \"Circuit breaker OPEN for stripe-api\" (x3)\n"
            "Recovery: No recovery - external API still failing in most recent lines.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: Processing payment request #98712\n"
            "[2/10] ERROR: HTTP 503 from https://api.stripe.com/v1/charges\n"
            "[3/10] WARN: Retry attempt 3/5 for payment charge\n"
            "[4/10] ERROR: Connection timed out: api.stripe.com:443\n"
            "[5/10] WARN: Circuit breaker OPEN for stripe-api\n"
            "[6/10] INFO: Queued payment #98712 for later retry\n"
            "[7/10] INFO: Processing payment request #98713\n"
            "[8/10] ERROR: HTTP 503 from https://api.stripe.com/v1/charges\n"
            "[9/10] WARN: Retry attempt 2/5 for payment charge\n"
            "[10/10] INFO: Queued payment #98713 for later retry"
        ),
        "expect_status": "degraded", "score_min": 30, "score_max": 65,
        "expect_action": "none", "expect_restart": False,
    },
]

MODEL_TIERS = {
    "high": ["qwen3:14b", "gemma4:latest", "phi4:latest", "deepseek-r1:14b"],
    "standard": ["llama3.1:8b", "qwen2.5:7b-instruct", "mistral:7b", "gemma3:4b"],
    "low": ["llama3.2:3b", "phi4-mini:latest"],
}


def _get_model_tier(model: str) -> str:
    for tier, models in MODEL_TIERS.items():
        if model in models:
            return tier
    return "unknown"


def _score_scenario(sc, result):
    """Score a single scenario result. Returns (points, notes_list)."""
    pts = 0
    notes = []

    # Status correctness (30 pts)
    if result.status == sc["expect_status"]:
        pts += 30
    elif sc["expect_status"] == "healthy" and result.status == "degraded":
        pts += 12
        notes.append(f"status: got {result.status}, expected {sc['expect_status']}")
    elif sc["expect_status"] == "critical" and result.status == "unhealthy":
        pts += 18
        notes.append(f"status: got {result.status}, expected {sc['expect_status']}")
    elif sc["expect_status"] == "degraded" and result.status in ("healthy", "unhealthy"):
        pts += 10
        notes.append(f"status: got {result.status}, expected {sc['expect_status']}")
    else:
        notes.append(f"status: got {result.status}, expected {sc['expect_status']}")

    # Health score range (30 pts)
    if sc["score_min"] <= result.health_score <= sc["score_max"]:
        pts += 30
    elif abs(result.health_score - sc["score_min"]) <= 10 or abs(result.health_score - sc["score_max"]) <= 10:
        pts += 15
        notes.append(f"score: {result.health_score}, expected {sc['score_min']}-{sc['score_max']}")
    else:
        notes.append(f"score: {result.health_score}, expected {sc['score_min']}-{sc['score_max']}")

    # Action correctness (20 pts)
    got_action = getattr(result, "recommended_action", "none") or "none"
    if got_action == sc["expect_action"]:
        pts += 20
    else:
        notes.append(f"action: got {got_action}, expected {sc['expect_action']}")

    # Restart recommendation (10 pts)
    got_restart = getattr(result, "restart_would_help", None)
    if got_restart == sc["expect_restart"]:
        pts += 10
    elif got_restart is not None:
        notes.append(f"restart: got {got_restart}, expected {sc['expect_restart']}")

    # Summary quality (10 pts)
    if result.summary and len(result.summary) > 15:
        pts += 10
    else:
        notes.append("summary too short")

    return pts, notes, got_action, got_restart


@router.post("/admin/benchmark/{model_name:path}")
async def run_benchmark(model_name: str):
    """Run full benchmark suite against a model. Hidden admin endpoint."""
    cfg = _get_cfg()
    from dockllama.ai_engine import evaluate, EvaluationContext
    import httpx as _httpx
    import time as _time
    import json as _json

    # Warmup
    try:
        async with _httpx.AsyncClient(timeout=cfg.ollama.timeout_seconds) as wc:
            await wc.post(
                f"{cfg.ollama.base_url}/api/generate",
                json={"model": model_name, "prompt": "Ready.", "stream": False,
                      "options": {"num_predict": 1}},
            )
    except Exception:
        pass

    results = {}
    times = []
    total_score = 0
    max_score = len(BENCHMARK_SCENARIOS) * 100

    for sc in BENCHMARK_SCENARIOS:
        sid = sc["id"]
        ctx = EvaluationContext(
            container_name=f"benchmark-{sid}",
            filtered_lines=[],
            structured_summary=sc["summary"],
            model=model_name,
        )
        t0 = _time.time()
        try:
            result, _ = await evaluate(ctx, cfg.ollama)
            ms = round((_time.time() - t0) * 1000)
            times.append(ms)
            pts, notes, got_action, got_restart = _score_scenario(sc, result)
            total_score += pts
            results[sid] = {
                "name": sc["name"], "difficulty": sc["difficulty"],
                "score": pts, "notes": "; ".join(notes) if notes else "Perfect",
                "response_ms": ms,
                "got_status": result.status, "got_score": result.health_score,
                "got_action": got_action, "got_restart": got_restart,
                "summary": (result.summary or "")[:200],
                "expected_status": sc["expect_status"],
                "expected_score_range": f"{sc['score_min']}-{sc['score_max']}",
                "passed": pts >= 70,
            }
        except Exception as e:
            results[sid] = {
                "name": sc["name"], "difficulty": sc["difficulty"],
                "score": 0, "notes": f"Error: {str(e)[:100]}",
                "response_ms": round((_time.time() - t0) * 1000), "passed": False,
            }

    avg_ms = round(sum(times) / len(times)) if times else 0
    pct = round(total_score / max_score * 100, 1) if max_score else 0
    tier = _get_model_tier(model_name)

    if pct >= 95: grade = "A+"
    elif pct >= 90: grade = "A"
    elif pct >= 80: grade = "B"
    elif pct >= 70: grade = "C"
    elif pct >= 60: grade = "D"
    else: grade = "F"

    # Save to DB
    conn = init_db(cfg.monitoring.db_path)
    try:
        results_json = _json.dumps({
            "benchmark_version": "2.0", "scenarios": results,
            "total_score": total_score, "max_score": max_score,
            "percentage": pct, "grade": grade, "avg_response_ms": avg_ms,
        })
        save_benchmark_result(conn, model_name, tier, total_score, max_score,
                              pct, grade, avg_ms, results_json)
    finally:
        conn.close()

    return {
        "model": model_name, "tier": tier,
        "total_score": total_score, "max_score": max_score,
        "percentage": pct, "grade": grade,
        "avg_response_ms": avg_ms, "results": results,
    }





@router.post("/admin/eval/pause")
async def pause_eval():
    """Pause the evaluation cycle (for benchmarking)."""
    from dockllama.main import eval_paused
    eval_paused.set()
    return {"status": "paused"}


@router.post("/admin/eval/resume")
async def resume_eval():
    """Resume the evaluation cycle."""
    from dockllama.main import eval_paused
    eval_paused.clear()
    return {"status": "resumed"}


@router.get("/admin/eval/status")
async def eval_status():
    """Check if evaluation cycle is paused."""
    from dockllama.main import eval_paused
    return {"paused": eval_paused.is_set()}


@router.post("/admin/benchmark-scenario/{scenario_id}/{model_name:path}")
async def run_benchmark_single(model_name: str, scenario_id: str):
    """Run a single benchmark scenario against a model."""
    cfg = _get_cfg()
    from dockllama.ai_engine import evaluate, EvaluationContext
    import httpx as _httpx
    import time as _time
    import json as _json

    sc = None
    for s in BENCHMARK_SCENARIOS:
        if s["id"] == scenario_id:
            sc = s
            break
    if not sc:
        return {"error": f"Unknown scenario {scenario_id}"}

    # Warmup
    try:
        async with _httpx.AsyncClient(timeout=cfg.ollama.timeout_seconds) as wc:
            await wc.post(
                f"{cfg.ollama.base_url}/api/generate",
                json={"model": model_name, "prompt": "Ready.", "stream": False,
                      "options": {"num_predict": 1}},
            )
    except Exception:
        pass

    ctx = EvaluationContext(
        container_name=f"benchmark-{scenario_id}",
        filtered_lines=[],
        structured_summary=sc["summary"],
        model=model_name,
    )
    t0 = _time.time()
    try:
        result, _ = await evaluate(ctx, cfg.ollama)
        ms = round((_time.time() - t0) * 1000)
        pts, notes, got_action, got_restart = _score_scenario(sc, result)
        return {
            "model": model_name, "scenario": scenario_id,
            "name": sc["name"], "difficulty": sc["difficulty"],
            "score": pts, "notes": "; ".join(notes) if notes else "Perfect",
            "response_ms": ms,
            "got_status": result.status, "got_score": result.health_score,
            "got_action": got_action, "got_restart": got_restart,
            "summary": (result.summary or "")[:200],
            "expected_status": sc["expect_status"],
            "expected_score_range": f"{sc['score_min']}-{sc['score_max']}",
            "passed": pts >= 70,
        }
    except Exception as e:
        return {
            "model": model_name, "scenario": scenario_id,
            "score": 0, "notes": f"Error: {str(e)[:200]}",
            "response_ms": round((_time.time() - t0) * 1000), "passed": False,
        }


@router.get("/admin/benchmark")
async def get_benchmarks():
    """Get all benchmark results. Hidden admin endpoint."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        return {"benchmarks": get_benchmark_results(conn), "tiers": MODEL_TIERS}
    finally:
        conn.close()

