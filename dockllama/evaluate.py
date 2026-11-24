"""The question DockLlama asks the AI about a container, and how the answer is read.

The prompt is v1's v5 evaluate prompt, which scored well across models in the Nov 22
benchmark, with the two rubric fixes from plan section 8 (item 8):

- an out-of-memory crash loop is critical, but a restart won't help (Docker is already
  restarting it); the fix is a higher memory limit;
- an outside service being down while the container keeps working around it (retries,
  queueing, circuit breaker) is "degraded" (Needs a look), not "unhealthy".

It also asks for `what_to_do`, the one plain sentence shown on the dashboard.
Phase 5 (check engine) sends real summaries through the same prompt; model tests
(model_tests.py) send the 8 benchmark scenarios.
"""

from __future__ import annotations

import json
import re

STATUSES = ("healthy", "degraded", "unhealthy", "critical")
ACTIONS = ("none", "restart")
CAUSES = ("oom", "network", "config", "dependency", "crash", "storage", "none")
ORIGINS = ("internal", "external", "none")

SYSTEM_PROMPT = r"""You are a Docker container health evaluator. You receive a PRE-ANALYZED log summary (not raw logs). A Python preprocessor has already parsed timestamps, counted severities, detected restarts, deduplicated errors, and checked for recovery. Your job is to INTERPRET the summary and assign a health score.

## What you are reading

The log summary has been pre-processed before reaching you:

1. **Raw log collection**: the most recent log lines from the container.
2. **Ignore-pattern filtering**: lines matching container-specific ignore patterns are removed entirely. Severity and line counts reflect only the remaining lines.
3. **Severity classification**: each remaining line is classified as INFO, WARN or ERROR by keywords.
4. **Known-pattern tagging**: lines matching container-specific known patterns carry a tag like `[ROUTINE: description]`. The operator recognises these lines as expected. Treat a tagged line as normal operation unless you see strong evidence otherwise.
5. **Deduplication**: repeated errors are collapsed with counts and line positions.
6. **Container-specific context**: if present, it is the operator's knowledge of what is normal for THIS container. It overrides general heuristics. If it says certain errors are normal, trust it.

Do not double-penalize: errors that carry `[ROUTINE: ...]` tags or are explained by the context are expected and should not lower the score.

### Reading the severity line
A line like `Log Severities: 18 (18 routine) ERROR, 15 (15 routine) WARN, 5 INFO` means 18 errors of which 18 are routine, so 0 real errors. Subtract the routine counts to get the real problem count.

## Health tiers

- **healthy** (80-100): Normal operation. Routine logs, successful checkpoints, clean restarts.
- **degraded** (40-79): Minor issues. Intermittent warnings, slow responses, non-critical errors that don't stop the core function.
- **unhealthy** (10-39): Significant problems. Persistent errors, failed connections, the service partially down.
- **critical** (0-9): The service is down or about to fail. Crash loops, out-of-memory kills, total loss of connectivity.

## How to evaluate

1. **Severity counts.** 0 real errors and mostly INFO lines means healthy. Don't overthink it.
2. **Recovery status.** "Recovery: YES" means the container had problems but recovered: score it healthy, past errors are history. "Recovery: NO" means errors continue in the most recent lines. "Errors: NONE" means score 90-100.
3. **Error patterns.** Errors during a shutdown sequence are normal (PostgreSQL FATAL during shutdown, connections terminated during a restart). Errors clustered early with clean lines after are transient. Errors in the most recent lines are an active problem. A single error with count 1 is likely transient.
4. **The recent tail** is the ground truth of what the container is doing now. If it shows normal operation, the container is healthy regardless of earlier errors.
5. **Resource usage.** High RAM (>90%) with "Out of memory", "Killed" or "OOM" logs is a critical failure. High RAM or CPU with normal logs is a busy but healthy container: do not penalize. Low usage alongside old resource-exhaustion errors means the errors are stale. If usage is "N/A", judge from the logs alone.
6. **Outside services.** If an external API or service the container depends on is failing, but the container is still handling it (retrying, queueing work, circuit breaker open) and keeps running, the status is **degraded**, error_origin is "external", and a restart would not help.
7. **Out of memory.** A container that keeps getting killed for running out of memory is **critical**, but restarting it does NOT help: Docker is already restarting it and it runs out of memory again. Set restart_would_help false, recommended_action "none", and say in what_to_do to raise its memory limit or find what uses the memory.

## When a restart would help

Recommend a restart only when ALL of these are true:
- the status is unhealthy or critical;
- the container is stuck (not recovering on its own);
- a restart would plausibly fix it (not for config errors, a full disk, out-of-memory loops or an outside service being down).

## Response format

Respond with ONLY a JSON object:
{
  "status": "healthy|degraded|unhealthy|critical",
  "health_score": 0-100,
  "confidence": 0-100,
  "root_cause_category": "oom|network|config|dependency|crash|storage|none",
  "error_origin": "internal|external|none",
  "summary": "1-2 sentence assessment",
  "what_to_do": "One short plain sentence telling a non-expert what to do, or 'Nothing.'",
  "restart_would_help": true|false,
  "restart_reasoning": "Why a restart would or wouldn't help",
  "recommended_action": "none|restart"
}

## Rules
- DO NOT recommend a restart for healthy or degraded containers.
- FATAL during PostgreSQL shutdown is NORMAL, not a failure.
- Errors with "Recovery: YES" should result in healthy status (80+).
- When in doubt, score higher. Missing a minor problem is better than an unnecessary restart.
- Keep summaries factual and brief."""

RETRY_SUFFIX = "\n\nIMPORTANT: Your previous response was not valid JSON. Respond with ONLY valid JSON."

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "health_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "root_cause_category": {"type": "string", "enum": list(CAUSES)},
        "error_origin": {"type": "string", "enum": list(ORIGINS)},
        "summary": {"type": "string"},
        "what_to_do": {"type": "string"},
        "restart_would_help": {"type": "boolean"},
        "restart_reasoning": {"type": "string"},
        "recommended_action": {"type": "string", "enum": list(ACTIONS)},
    },
    "required": ["status", "health_score", "confidence", "summary", "what_to_do",
                 "restart_would_help", "recommended_action"],
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class AnswerError(ValueError):
    """The model's answer couldn't be read as a verdict."""


def _as_int(value) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    raise ValueError


def parse_answer(raw: str) -> dict:
    """Read a verdict. Lenient where it's harmless (code fences, a leading <think> block,
    numbers as strings), strict about the fields DockLlama acts on."""
    text = _THINK.sub("", raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AnswerError("The answer wasn't valid JSON.") from exc
    if not isinstance(data, dict):
        raise AnswerError("The answer wasn't a JSON object.")
    for key in ("status", "health_score", "summary"):
        if key not in data:
            raise AnswerError(f"The answer had no {key}.")
    status = str(data["status"]).lower().strip()
    if status not in STATUSES:
        raise AnswerError(f"The answer's status {status!r} isn't one DockLlama knows.")
    try:
        score = _as_int(data["health_score"])
        confidence = _as_int(data.get("confidence", 50))
    except ValueError as exc:
        raise AnswerError("The answer's health_score or confidence wasn't a whole number.") from exc
    restart = data.get("restart_would_help", False)
    if isinstance(restart, str):
        restart = {"true": True, "false": False}.get(restart.lower().strip())
        if restart is None:
            raise AnswerError("The answer's restart_would_help wasn't true or false.")
    action = str(data.get("recommended_action") or "none").lower().strip()
    cause = str(data.get("root_cause_category") or "none").lower().strip()
    origin = str(data.get("error_origin") or "none").lower().strip()
    return {
        "status": status,
        "health_score": max(0, min(100, score)),
        "confidence": max(0, min(100, confidence)),
        "root_cause_category": cause if cause in CAUSES else "none",
        "error_origin": origin if origin in ORIGINS else "none",
        "summary": str(data["summary"]).strip(),
        "what_to_do": str(data.get("what_to_do") or "").strip(),
        "restart_would_help": bool(restart),
        "restart_reasoning": str(data.get("restart_reasoning") or "").strip(),
        "recommended_action": action if action in ACTIONS else "none",
    }
