"""Per-model Advanced settings (plan section 8B): validation against what the model supports.

Stored as JSON with only the keys the user changed; a missing key means "recommended".
The page greys out options the model doesn't support; this module enforces the same rules,
so the API can't be used to set them anyway.
"""

from __future__ import annotations

from .ollama import DEFAULT_NUM_CTX, ModelDetails, RequestSettings

MIN_CTX = 4096  # DockLlama's own prompts need about 3–5K tokens
CTX_CEILING_UNKNOWN = 131072  # if a model doesn't report its maximum


class AdvancedError(ValueError):
    pass


def ctx_limit(model: ModelDetails) -> int:
    return model.max_context or CTX_CEILING_UNKNOWN


def options_for(model: ModelDetails) -> dict:
    """What the page may offer for this model."""
    limit = ctx_limit(model)
    steps = [s for s in (4096, 8192, 16384, 32768, 65536, 131072, 262144) if s <= limit]
    if limit not in steps:
        steps.append(limit)
    return {
        "thinking": model.thinking,  # none (greyed out) | toggle | levels
        "think_levels": model.think_levels,
        "context_steps": steps,
        "context_max": limit,
        "context_min": min(MIN_CTX, limit),
        "context_recommended": min(DEFAULT_NUM_CTX, limit),
    }


def validate(model: ModelDetails, raw: dict) -> dict:
    clean: dict = {}
    for key, value in raw.items():
        if value is None:
            continue  # None = back to recommended
        if key == "think":
            if model.thinking == "none":
                raise AdvancedError("This model doesn't think, so thinking can't be set.")
            if model.thinking == "toggle" and not isinstance(value, bool):
                raise AdvancedError("For this model, thinking is either on or off.")
            if model.thinking == "levels":
                if value is False and "off" in model.think_levels:
                    value = "off"
                if value not in model.think_levels:
                    raise AdvancedError(f"For this model, thinking must be one of: {', '.join(model.think_levels)}.")
                if value == "off":
                    value = False
            clean[key] = value
        elif key == "num_ctx":
            limit = ctx_limit(model)
            if not isinstance(value, int) or isinstance(value, bool) or not (min(MIN_CTX, limit) <= value <= limit):
                raise AdvancedError(f"Context size must be between {min(MIN_CTX, limit):,} and {limit:,} for this model.")
            clean[key] = value
        elif key == "keep_alive":
            if value in ("always", "unload"):
                clean[key] = value
            elif isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1440:
                clean[key] = value  # minutes
            else:
                raise AdvancedError("Keep loaded must be always, unload, or 1–1440 minutes.")
        elif key == "num_predict":
            if not isinstance(value, int) or isinstance(value, bool) or not (100 <= value <= 8192):
                raise AdvancedError("Max answer length must be 100–8,192 tokens.")
            clean[key] = value
        elif key == "temperature":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 <= value <= 1):
                raise AdvancedError("Temperature must be between 0 and 1.")
            clean[key] = float(value)
        elif key == "timeout":
            if not isinstance(value, int) or isinstance(value, bool) or not (30 <= value <= 900):
                raise AdvancedError("Timeout must be 30–900 seconds.")
            clean[key] = value
        elif key in ("retry", "schema_format"):
            if not isinstance(value, bool):
                raise AdvancedError(f"{key} must be true or false.")
            clean[key] = value
        else:
            raise AdvancedError(f"Unknown setting {key!r}.")
    return clean


def warnings_for(model: ModelDetails, clean: dict) -> list[str]:
    """Combinations known to cause trouble (shown, not blocked)."""
    out = []
    thinking_on = clean.get("think") not in (None, False)
    # Same default as ollama.build_generate_request: 2,000 tokens when thinking is on.
    if thinking_on and clean.get("num_predict", 2000) < 1500:
        out.append("Thinking uses the same budget as the answer, so the answer may get cut off. Raise the max answer length.")
    if clean.get("num_ctx", DEFAULT_NUM_CTX) > 32768:
        out.append("Large contexts use a lot more memory; the model may spill to the CPU and slow down.")
    if "num_ctx" in clean:
        out.append("Other apps using this Ollama server with a different context size will cause model reloads.")
    return out


def to_request(clean: dict, interval_minutes: int = 10) -> RequestSettings:
    keep = clean.get("keep_alive")
    if keep == "always":
        keep_alive: str | int | None = -1
    elif keep == "unload":
        keep_alive = 0
    elif isinstance(keep, int):
        keep_alive = f"{keep}m"
    else:
        keep_alive = f"{max(15, interval_minutes * 2)}m"  # recommended: longer than the check interval
    return RequestSettings(
        think=clean.get("think"),
        num_ctx=clean.get("num_ctx"),
        keep_alive=keep_alive,
        num_predict=clean.get("num_predict"),
        temperature=clean.get("temperature"),
        timeout=clean.get("timeout"),
        schema_format=clean.get("schema_format", True),
    )
