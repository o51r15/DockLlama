"""AI evaluation engine — sends filtered logs to Ollama and parses structured responses."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import sqlite3
from pydantic import BaseModel, field_validator

from dockllama.config import OllamaConfig

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"
DEFAULT_PROMPT_VERSION = "v5_evaluate"


class EvaluationResult(BaseModel):
    """Structured response from the LLM."""
    status: str  # "healthy", "degraded", "unhealthy", or "critical"
    health_score: int  # 0-100 numeric health score
    confidence: int
    root_cause_category: str = "none"
    error_origin: str = "none"
    summary: str
    restart_would_help: bool = False
    restart_reasoning: str = ""
    recommended_action: str = "none"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("healthy", "degraded", "unhealthy", "critical"):
            raise ValueError(f"Invalid status: {v}")
        return v

    @field_validator("health_score")
    @classmethod
    def validate_health_score(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("root_cause_category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        v = v.lower().strip()
        valid = {"oom", "network", "config", "dependency", "crash", "storage", "none"}
        return v if v in valid else "none"

    @field_validator("error_origin")
    @classmethod
    def validate_origin(cls, v: str) -> str:
        v = v.lower().strip()
        return v if v in ("internal", "external", "none") else "none"

    @field_validator("recommended_action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        v = v.lower().strip()
        return v if v in ("none", "restart") else "none"


@dataclass
class EvaluationContext:
    """Everything needed to evaluate a container's logs."""
    container_name: str
    filtered_lines: list[str]
    model: str
    structured_summary: Optional[str] = None  # from log_analyzer
    baseline_sample: Optional[str] = None
    context_prompt: Optional[str] = None
    examples: list[dict] = field(default_factory=list)
    prompt_version: str = DEFAULT_PROMPT_VERSION


def _load_prompt(version: str) -> str:
    """Load a versioned prompt template."""
    path = PROMPT_DIR / f"{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text().strip()




def _load_prompt_from_db(prompt_type: str, db_path: str = None) -> str | None:
    """Try to load the active base prompt from DB. Returns None if not available."""
    if not db_path:
        return None
    try:
        import sqlite3 as _sql
        conn = _sql.connect(db_path)
        row = conn.execute(
            "SELECT content FROM base_prompts WHERE prompt_type = ? AND is_active = 1 LIMIT 1",
            (prompt_type,),
        ).fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    return None


def _load_prompt_with_fallback(prompt_type: str, file_version: str, db_path: str = None) -> str:
    """Load prompt from DB if available, otherwise fall back to file."""
    db_prompt = _load_prompt_from_db(prompt_type, db_path)
    if db_prompt:
        return db_prompt
    return _load_prompt(file_version)

def _build_messages(ctx: EvaluationContext, db_path: str = None) -> tuple[str, str]:
    """Build system and user prompts for the LLM."""
    system_prompt = _load_prompt_with_fallback("eval", ctx.prompt_version, db_path)

    # Append container-specific context if provided
    if ctx.context_prompt:
        system_prompt += "\n\n## Container-Specific Context\n" + ctx.context_prompt

    # Append few-shot examples if provided
    if ctx.examples:
        parts = ["\n\n## Reference Examples"]
        parts.append("The following are known scenarios with their correct evaluations. Use these to calibrate your scoring.")
        for ex in ctx.examples:
            label = ex.get("label", "Example")
            snippet = ex.get("log_snippet", "")
            score = ex.get("correct_score", "N/A")
            status = ex.get("correct_status", "N/A")
            reasoning = ex.get("reasoning", "")
            parts.append(f"\n### Example: {label}")
            parts.append(f"Log sample:\n---\n{snippet}\n---")
            parts.append(f"Correct assessment: {status}, score {score}")
            if reasoning:
                parts.append(f"Reasoning: {reasoning}")
        system_prompt += "\n".join(parts)

    # v5: use structured summary from log_analyzer if available
    if ctx.structured_summary:
        return system_prompt, ctx.structured_summary

    # Fallback: old-style filtered lines (v4 compat)
    user_parts = [f"Container: {ctx.container_name}"]

    if ctx.baseline_sample:
        user_parts.append(
            f"\nFor reference, here is what normal/healthy logs look like for this container:\n"
            f"---\n{ctx.baseline_sample}\n---"
        )

    user_parts.append(f"\nRecent log lines ({len(ctx.filtered_lines)} lines, pre-filtered to WARN/ERROR/unknown only):")
    user_parts.append("Line 1 is the OLDEST, line {0} is the MOST RECENT. Weight recent lines more heavily.".format(len(ctx.filtered_lines)))
    user_parts.append("---")
    numbered = [f"[{i+1}/{len(ctx.filtered_lines)}] {line}" for i, line in enumerate(ctx.filtered_lines)]
    user_parts.append("\n".join(numbered))
    user_parts.append("---")

    return system_prompt, "\n".join(user_parts)


def _make_fallback(container_name: str, reason: str) -> EvaluationResult:
    """Return a safe fallback result when the LLM fails."""
    return EvaluationResult(
        status="healthy",
        health_score=50,
        confidence=0,
        root_cause_category="none",
        error_origin="none",
        summary=f"Evaluation failed: {reason}. Failing open (assuming healthy).",
        restart_would_help=False,
        restart_reasoning="Evaluation failed, cannot determine.",
        recommended_action="none",
    )


async def evaluate(
    ctx: EvaluationContext,
    ollama_config: OllamaConfig,
) -> tuple[EvaluationResult, str]:
    """
    Send filtered logs to Ollama and parse the structured response.

    Returns (result, prompt_version).
    Fails open on any error — returns a healthy fallback rather than crashing.
    """
    if not ctx.filtered_lines and not ctx.structured_summary:
        return EvaluationResult(
            status="healthy",
            health_score=95,
            confidence=95,
            root_cause_category="none",
            error_origin="none",
            summary="No warning or error lines detected in recent logs.",
            restart_would_help=False,
            restart_reasoning="No issues detected.",
            recommended_action="none",
        ), ctx.prompt_version

    system_prompt, user_prompt = _build_messages(ctx)

    try:
        async with httpx.AsyncClient(timeout=ollama_config.timeout_seconds) as client:
            response = await client.post(
                f"{ollama_config.base_url}/api/generate",
                json={
                    "model": ctx.model,
                    "system": system_prompt,
                    "prompt": user_prompt,
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 500,
                    },
                },
            )
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Ollama timeout for %s", ctx.container_name)
        return _make_fallback(ctx.container_name, "Ollama request timed out"), ctx.prompt_version
    except httpx.HTTPError as e:
        logger.warning("Ollama HTTP error for %s: %s", ctx.container_name, e)
        return _make_fallback(ctx.container_name, f"Ollama HTTP error: {e}"), ctx.prompt_version

    # Parse response
    try:
        data = response.json()

        # Check for Ollama-level error (e.g. GPU contention, model unloaded)
        if "error" in data:
            ollama_err = data["error"]
            logger.warning("Ollama returned error for %s: %s — retrying in 5s", ctx.container_name, ollama_err)
            await asyncio.sleep(5)
            try:
                async with httpx.AsyncClient(timeout=ollama_config.timeout_seconds) as client:
                    response = await client.post(
                        f"{ollama_config.base_url}/api/generate",
                        json={
                            "model": ctx.model,
                            "system": system_prompt,
                            "prompt": user_prompt,
                            "format": "json",
                            "stream": False,
                            "think": False,
                            "options": {"temperature": 0.1, "num_predict": 500},
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    if "error" in data:
                        return _make_fallback(ctx.container_name, f"Ollama error after retry: {data['error']}"), ctx.prompt_version
            except Exception as retry_err:
                return _make_fallback(ctx.container_name, f"Ollama retry failed: {retry_err}"), ctx.prompt_version

        raw_text = data.get("response", "")
        parsed = json.loads(raw_text)
        result = EvaluationResult(**parsed)
        return result, ctx.prompt_version
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(
            "Failed to parse Ollama response for %s: %s\nRaw: %s",
            ctx.container_name, e, raw_text[:500] if 'raw_text' in dir() else "no response",
        )
        # Retry once for bad JSON
        try:
            async with httpx.AsyncClient(timeout=ollama_config.timeout_seconds) as client:
                response = await client.post(
                    f"{ollama_config.base_url}/api/generate",
                    json={
                        "model": ctx.model,
                        "system": system_prompt,
                        "prompt": user_prompt + "\n\nIMPORTANT: Your previous response was not valid JSON. Respond with ONLY valid JSON.",
                        "format": "json",
                        "stream": False,
                        "think": False,
                        "options": {"temperature": 0.0, "num_predict": 300},
                    },
                )
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    return _make_fallback(ctx.container_name, f"Ollama error on retry: {data['error']}"), ctx.prompt_version
                raw_text = data.get("response", "")
                parsed = json.loads(raw_text)
                result = EvaluationResult(**parsed)
                return result, ctx.prompt_version
        except Exception:
            pass

        return _make_fallback(ctx.container_name, f"Invalid LLM response: {e}"), ctx.prompt_version




# ─── Log Evaluation (Full / Deep Analysis) ─────────────────────

from collections import Counter as _Counter
import re as _re


def _deduplicate_logs(lines: list[str]) -> str:
    """Deduplicate log lines by normalized message, preserving counts and time range."""
    pattern_map = {}

    for i, line in enumerate(lines):
        normalized = _re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*\s*', '', line)
        normalized = _re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', normalized)
        normalized = _re.sub(r'\b\d{1,5}\b', '<N>', normalized)
        normalized = normalized.strip()

        if not normalized:
            continue

        if normalized not in pattern_map:
            pattern_map[normalized] = {
                "count": 0,
                "first_idx": i,
                "last_idx": i,
                "sample": line.strip(),
            }
        pattern_map[normalized]["count"] += 1
        pattern_map[normalized]["last_idx"] = i

    sorted_patterns = sorted(pattern_map.values(), key=lambda p: p["first_idx"])

    parts = [f"Deduplicated log patterns ({len(lines)} total lines -> {len(sorted_patterns)} unique patterns):", ""]
    for p in sorted_patterns:
        count_str = f" (x{p['count']})" if p['count'] > 1 else ""
        span = f" [lines {p['first_idx']+1}-{p['last_idx']+1}]" if p['count'] > 1 else f" [line {p['first_idx']+1}]"
        parts.append(f"{p['sample']}{count_str}{span}")

    return "\n".join(parts)


def _chunk_logs(lines: list[str], chunk_size: int = 400) -> list[list[str]]:
    """Split log lines into chunks for multi-pass evaluation."""
    chunks = []
    for i in range(0, len(lines), chunk_size):
        chunks.append(lines[i:i + chunk_size])
    return chunks


async def log_evaluate(
    container_name: str,
    raw_lines: list[str],
    hours: int,
    output_format: str,
    model: str,
    ollama_config,
    db_path: str = None,
    context_prompt: str = None,
) -> dict:
    """
    Perform a deep log evaluation on unfiltered container logs.

    Adaptive strategy based on line count:
    - <=500: send all raw lines directly
    - 500-2000: deduplicate patterns with counts
    - 2000+: chunk, summarize each, then synthesize

    Returns dict with: result, strategy, line_count, model, prompt_name
    """
    line_count = len(raw_lines)

    # Load prompt from DB or file
    system_prompt = _load_prompt_with_fallback("log_eval", "v1_log_evaluate", db_path)
    prompt_name = "log_eval"

    # Try to get the active prompt name
    if db_path:
        try:
            _conn = sqlite3.connect(db_path)
            row = _conn.execute(
                "SELECT name FROM base_prompts WHERE prompt_type = 'log_eval' AND is_active = 1 LIMIT 1"
            ).fetchone()
            if row:
                prompt_name = row[0]
            _conn.close()
        except Exception:
            pass

    if context_prompt:
        system_prompt += "\n\n## Container-Specific Context\n" + context_prompt

    # Add format instruction
    format_instruction = {
        "report": "\n\nRespond in REPORT format (structured JSON as described above).",
        "freeform": "\n\nRespond in FREEFORM format (detailed narrative analysis in plain text).",
        "json": "\n\nRespond in JSON format (structured JSON as described above).",
    }
    system_prompt += format_instruction.get(output_format, format_instruction["report"])

    use_json_format = output_format in ("report", "json")

    # Determine strategy
    if line_count == 0:
        return {
            "result": {"summary": "No logs found for the specified time period.", "severity": "clean", "findings": []},
            "strategy": "empty",
            "line_count": 0,
            "model": model,
            "prompt_name": prompt_name,
        }

    if line_count <= 500:
        strategy = "direct"
        user_prompt = f"Container: {container_name}\nTime window: last {hours} hour(s)\nTotal lines: {line_count}\n\n--- RAW LOGS ---\n"
        user_prompt += "\n".join(raw_lines)
        user_prompt += "\n--- END LOGS ---"

    elif line_count <= 2000:
        strategy = "deduplicated"
        user_prompt = f"Container: {container_name}\nTime window: last {hours} hour(s)\n\n"
        user_prompt += _deduplicate_logs(raw_lines)

    else:
        strategy = "chunked"
        chunks = _chunk_logs(raw_lines, chunk_size=400)
        logger.info("Log evaluate %s: chunking %d lines into %d chunks", container_name, line_count, len(chunks))

        # Phase 1: summarize each chunk
        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            chunk_prompt = (
                f"Container: {container_name} -- Chunk {i+1}/{len(chunks)}\n"
                f"Time window: last {hours} hour(s) (this is chunk {i+1} of {len(chunks)})\n"
                f"Lines {i*400+1}-{min((i+1)*400, line_count)} of {line_count}\n\n"
                "Summarize the key findings, errors, warnings, and patterns in this chunk. "
                "Be specific about error messages, counts, and patterns. This summary will be combined with other chunks.\n\n"
                "--- CHUNK LOGS ---\n"
            )
            chunk_prompt += "\n".join(chunk)
            chunk_prompt += "\n--- END CHUNK ---"

            try:
                async with httpx.AsyncClient(timeout=ollama_config.timeout_seconds) as client:
                    resp = await client.post(
                        f"{ollama_config.base_url}/api/generate",
                        json={
                            "model": model,
                            "system": "You are a log analysis assistant. Summarize the key findings from this log chunk concisely but thoroughly. Focus on errors, warnings, patterns, and anything noteworthy.",
                            "prompt": chunk_prompt,
                            "stream": False,
                            "options": {"temperature": 0.1, "num_predict": 1024},
                        },
                    )
                    resp.raise_for_status()
                    chunk_summary = resp.json().get("response", "")
                    chunk_summaries.append(f"=== Chunk {i+1}/{len(chunks)} (lines {i*400+1}-{min((i+1)*400, line_count)}) ===\n{chunk_summary}")
            except Exception as e:
                chunk_summaries.append(f"=== Chunk {i+1}/{len(chunks)} === [Analysis failed: {e}]")

        # Phase 2: synthesize
        user_prompt = (
            f"Container: {container_name}\n"
            f"Time window: last {hours} hour(s)\n"
            f"Total lines: {line_count} (analyzed in {len(chunks)} chunks)\n\n"
            "The following are summaries from analyzing each chunk of logs. "
            "Synthesize these into a comprehensive analysis, identifying patterns that span multiple chunks.\n\n"
        )
        user_prompt += "\n\n".join(chunk_summaries)

    # Make the LLM call
    try:
        payload = {
            "model": model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 4096},
        }
        if use_json_format:
            payload["format"] = "json"

        async with httpx.AsyncClient(timeout=max(ollama_config.timeout_seconds, 120)) as client:
            resp = await client.post(
                f"{ollama_config.base_url}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            raw_text = resp.json().get("response", "")
    except Exception as e:
        logger.exception("Log evaluate failed for %s", container_name)
        return {
            "result": {"summary": f"Evaluation failed: {e}", "severity": "unknown", "findings": []},
            "strategy": strategy,
            "line_count": line_count,
            "model": model,
            "prompt_name": prompt_name,
        }

    # Parse result
    if output_format == "freeform":
        result = {"text": raw_text, "format": "freeform"}
    else:
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end > start:
                try:
                    result = json.loads(raw_text[start:end + 1])
                except json.JSONDecodeError:
                    result = {"text": raw_text, "format": "freeform", "parse_error": True}
            else:
                result = {"text": raw_text, "format": "freeform", "parse_error": True}

    return {
        "result": result,
        "strategy": strategy,
        "line_count": line_count,
        "model": model,
        "prompt_name": prompt_name,
    }

if __name__ == "__main__":
    import asyncio

    async def test():
        cfg = OllamaConfig(base_url="http://localhost:11434")
        ctx = EvaluationContext(
            container_name="test-container",
            filtered_lines=[
                "WARN\tprowlarr\tprowlarr/crawler.go:187\tprowlarr: search failed\t{\"error\": \"API returned status 400\"}",
                "ERROR\tapp\tserver.go:55\tfailed to connect to database\t{\"error\": \"connection refused\"}",
            ],
            model=cfg.default_model,
        )
        result, version = await evaluate(ctx, cfg)
        print(f"Status: {result.status}")
        print(f"Confidence: {result.confidence}")
        print(f"Category: {result.root_cause_category}")
        print(f"Summary: {result.summary}")
        print(f"Action: {result.recommended_action}")
        print(f"Prompt version: {version}")

    asyncio.run(test())
