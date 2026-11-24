"""The one place DockLlama talks to Ollama.

Covers finding a server, checking it's really Ollama and new enough, reading model details,
downloading models, and (from phase 3b on) running requests. Every request goes through
`build_generate_request`, so think / num_ctx / keep_alive / format are set consistently
(the v1 problems listed in notes/OLLAMA_FINDINGS_2026-11.md).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import httpx

MIN_VERSION = (0, 9, 0)  # `think` arrived in Ollama 0.9.0
DEFAULT_PORT = 11434
DISCOVERY_CANDIDATES = [
    "http://host.docker.internal:11434",  # Ollama on the same machine (compose adds this name)
    "http://ollama:11434",  # an Ollama container on the same Docker network
    "http://172.17.0.1:11434",  # the default Docker bridge gateway
]


class OllamaError(Exception):
    """A problem with the AI server, worded for the user. `kind` lets the page show the right help."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # unreachable | not_ollama | too_old | no_model | bad_request | timeout


# --- addresses ------------------------------------------------------------------


def normalize_url(text: str) -> str:
    """Turn what a user types ('192.168.1.5', 'thor:11434', 'http://x/') into a base URL."""
    text = (text or "").strip()
    if not text:
        raise OllamaError("bad_request", "Type the address of the machine running Ollama.")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OllamaError("bad_request", f"“{text}” doesn't look like an address.")
    host = parts.hostname
    if ":" in host:  # IPv6
        host = f"[{host}]"
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as exc:
        raise OllamaError("bad_request", "The port number in that address isn't valid.") from exc
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}:{port}{path}"


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


# --- model details --------------------------------------------------------------


MIN_USABLE_CONTEXT = 4096  # DockLlama's checks use about 4K tokens


@dataclass
class ModelDetails:
    name: str
    digest: str
    modified_at: str
    size: int  # bytes on disk
    family: str
    parameter_size: str
    capabilities: list[str]
    max_context: int | None
    thinking: str  # "none" | "toggle" | "levels"
    think_levels: list[str] = field(default_factory=list)
    is_cloud: bool = False
    # Built on Alibaba's Qwen models. Shown as a label; can be left out of suggestions
    # (setting ai.exclude_qwen). DockLlama never gives any model tools, and Qwen-based models
    # in particular must not get tools until more is known (see docs/ai-and-models.md).
    qwen_based: bool = False

    @property
    def usable(self) -> bool:
        return self.unusable_reason is None

    @property
    def unusable_reason(self) -> str | None:
        if self.is_cloud:
            return "Cloud model (needs an ollama.com account and may cost money)"
        if "completion" not in self.capabilities:
            if "embedding" in self.capabilities:
                return "Embedding model: it can't write answers"
            return "This model can't write answers"
        if self.max_context is not None and self.max_context < MIN_USABLE_CONTEXT:
            return "Its memory (context) is too small for DockLlama's checks"
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["usable"] = self.usable
        d["unusable_reason"] = self.unusable_reason
        return d


def is_qwen_based(name: str, details: dict, info: dict) -> bool:
    """True when the model's name, family, architecture or recorded base model mentions Qwen.

    Fine-tunes often get a new name but keep the Qwen architecture (qwen2, qwen3, qwen3moe...)
    and usually record the base model under general.base_model.*."""
    fields: list = [name, details.get("family"), *(details.get("families") or []),
                    info.get("general.architecture"), info.get("general.basename")]
    fields += [v for k, v in info.items() if k.startswith("general.base_model.")]
    return any(isinstance(f, str) and "qwen" in f.lower() for f in fields)


def details_from(tag: dict, show: dict) -> ModelDetails:
    """Combine an /api/tags entry and the model's /api/show answer."""
    info = show.get("model_info") or {}
    arch = info.get("general.architecture")
    max_ctx = info.get(f"{arch}.context_length") if arch else None
    caps = list(show.get("capabilities") or [])
    levels: list[str] = []
    thinking = "none"
    t = show.get("thinking")
    if t and isinstance(t.get("values"), list):
        values = t["values"]
        strings = [v for v in values if isinstance(v, str)]
        if strings:
            # Some level models can also be switched off (muse-glimmer lists False first).
            thinking, levels = "levels", (["off"] if False in values else []) + strings
        elif True in values:
            thinking = "toggle"
    elif "thinking" in caps:
        thinking = "toggle"  # some models (granite4.2, deepseek-r1) list the capability without details
    details = tag.get("details") or {}
    return ModelDetails(
        name=tag["name"],
        digest=tag.get("digest", ""),
        modified_at=tag.get("modified_at", ""),
        size=int(tag.get("size") or 0),
        family=details.get("family", ""),
        parameter_size=details.get("parameter_size", ""),
        capabilities=caps,
        max_context=int(max_ctx) if isinstance(max_ctx, (int, float)) else None,
        thinking=thinking,
        think_levels=levels,
        is_cloud=bool(tag.get("remote_host")) or tag["name"].endswith("-cloud") or ":cloud" in tag["name"],
        qwen_based=is_qwen_based(tag["name"], details, info),
    )


def inventory_fingerprint(tags: list[dict]) -> list[str]:
    """One short hash per installed model: name + content digest + when it was pulled *here*.

    Pull times differ between machines even for identical models, so the set identifies an
    Ollama install without relying on its IP address (plan section 8C).
    """
    return sorted(
        hashlib.sha256(f"{t.get('name')}|{t.get('digest')}|{t.get('modified_at')}".encode()).hexdigest()[:16]
        for t in tags
    )


def fingerprint_overlap(stored: list[str], current: list[str]) -> float:
    """Share of the stored server's entries still present (0..1)."""
    if not stored or not current:
        return 0.0
    return len(set(stored) & set(current)) / len(set(stored))


# --- requests ------------------------------------------------------------------


@dataclass
class RequestSettings:
    """Per-model settings (Settings → AI → Advanced). None means DockLlama's recommended value."""

    think: bool | str | None = None
    num_ctx: int | None = None
    keep_alive: str | int | None = None
    num_predict: int | None = None
    temperature: float | None = None
    timeout: int | None = None
    schema_format: bool = True


DEFAULT_NUM_CTX = 8192
DEFAULT_KEEP_ALIVE = "30m"
DEFAULT_TEMPERATURE = 0.1


def recommended_think(model: ModelDetails) -> bool | str | None:
    """Thinking off where possible; the lowest level where it can't be turned off (gpt-oss)."""
    if model.thinking == "levels":
        if "off" in model.think_levels:
            return False
        return "low" if "low" in model.think_levels else model.think_levels[0]
    if model.thinking == "toggle":
        return False
    return None  # model doesn't think: send no think field at all


def build_generate_request(
    model: ModelDetails,
    prompt: str,
    *,
    system: str | None = None,
    schema: dict | None = None,
    settings: RequestSettings | None = None,
) -> dict:
    s = settings or RequestSettings()
    num_ctx = s.num_ctx or DEFAULT_NUM_CTX
    if model.max_context:
        num_ctx = min(num_ctx, model.max_context)
    think = s.think if s.think is not None else recommended_think(model)
    if think == "off":
        think = False
    num_predict = s.num_predict or (2000 if think not in (None, False) else 500)
    payload: dict = {
        "model": model.name,
        "prompt": prompt,
        "stream": False,
        "keep_alive": s.keep_alive if s.keep_alive is not None else DEFAULT_KEEP_ALIVE,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": s.temperature if s.temperature is not None else DEFAULT_TEMPERATURE,
        },
    }
    if model.thinking != "none" and think is not None:
        payload["think"] = think  # top level, never inside options (v1's digest bug)
    if system:
        payload["system"] = system
    if schema is not None:
        payload["format"] = schema if (s.schema_format and not model.is_cloud) else "json"
    return payload


# --- client --------------------------------------------------------------------


# Tests set this to an httpx.MockTransport (a fake Ollama). Always None in real use.
TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _http(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout, trust_env=False,
                                 transport=TEST_TRANSPORT)

    async def _get_json(self, path: str, timeout: float | None = None) -> dict:
        try:
            async with self._http(timeout) as http:
                r = await http.get(path)
        except httpx.TimeoutException as exc:
            raise OllamaError("unreachable", f"{self.base_url} didn't answer in time.") from exc
        except httpx.HTTPError as exc:
            raise OllamaError("unreachable", f"Nothing answered at {self.base_url}.") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise OllamaError("not_ollama", f"Something answered at {self.base_url}, but it isn't Ollama. Check the port number.") from exc
        if r.status_code != 200:
            raise OllamaError("not_ollama", f"Something answered at {self.base_url}, but it isn't Ollama.")
        return data

    async def version(self, timeout: float | None = None) -> str:
        data = await self._get_json("/api/version", timeout)
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str):
            raise OllamaError("not_ollama", f"Something answered at {self.base_url}, but it isn't Ollama. Check the port number.")
        return version

    async def check(self, timeout: float | None = None) -> str:
        """Version string if this is a usable Ollama, else OllamaError."""
        version = await self.version(timeout)
        parsed = parse_version(version)
        if parsed is not None and parsed != (0, 0, 0) and parsed < MIN_VERSION:
            need = ".".join(map(str, MIN_VERSION))
            raise OllamaError("too_old", f"This Ollama is version {version}. DockLlama needs {need} or newer. Update Ollama on that machine.")
        return version

    async def tags(self) -> list[dict]:
        data = await self._get_json("/api/tags")
        return list(data.get("models") or [])

    async def ps(self) -> list[dict]:
        data = await self._get_json("/api/ps")
        return list(data.get("models") or [])

    async def show(self, name: str) -> dict:
        try:
            async with self._http() as http:
                r = await http.post("/api/show", json={"model": name})
        except httpx.HTTPError as exc:
            raise OllamaError("unreachable", f"Nothing answered at {self.base_url}.") from exc
        if r.status_code == 404:
            raise OllamaError("no_model", f"The model {name} isn't installed on this server.")
        if r.status_code != 200:
            raise OllamaError("bad_request", f"Ollama couldn't describe {name} ({r.status_code}).")
        return r.json()

    async def models(self) -> list[ModelDetails]:
        tags = await self.tags()
        shows = await asyncio.gather(*(self.show(t["name"]) for t in tags), return_exceptions=True)
        result = []
        for tag, show in zip(tags, shows):
            result.append(details_from(tag, show if isinstance(show, dict) else {}))
        return result

    async def pull(self, name: str) -> AsyncIterator[dict]:
        """Download a model. Yields Ollama's progress events: {status, total?, completed?}."""
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(30.0, read=600.0),
                                         trust_env=False, transport=TEST_TRANSPORT) as http:
                async with http.stream("POST", "/api/pull", json={"model": name, "stream": True}) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="replace")
                        raise OllamaError("bad_request", _pull_error(name, body))
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if "error" in event:
                            raise OllamaError("bad_request", _pull_error(name, event["error"]))
                        yield event
        except httpx.HTTPError as exc:
            raise OllamaError("unreachable", f"The download from {self.base_url} stopped: {exc}") from exc

    async def unload(self, name: str) -> None:
        """Free the model's memory now (keep_alive 0). Best effort: never raises."""
        try:
            async with self._http(30) as http:
                await http.post("/api/generate", json={"model": name, "keep_alive": 0})
        except httpx.HTTPError:
            pass

    async def generate(self, payload: dict, timeout: float = 120.0) -> dict:
        try:
            async with self._http(timeout) as http:
                r = await http.post("/api/generate", json=payload)
        except httpx.TimeoutException as exc:
            raise OllamaError("timeout", f"The model took longer than {int(timeout)} seconds to answer.") from exc
        except httpx.HTTPError as exc:
            raise OllamaError("unreachable", f"Nothing answered at {self.base_url}.") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise OllamaError("bad_request", "Ollama sent back something that isn't JSON.") from exc
        if r.status_code != 200 or "error" in data:
            raise OllamaError("bad_request", f"Ollama said: {data.get('error', r.status_code)}")
        return data


def _pull_error(name: str, text: str) -> str:
    if "not found" in text.lower() or "file does not exist" in text.lower():
        return f"Ollama's library has no model called {name}."
    return f"Ollama couldn't download {name}: {text.strip()[:200]}"


def truncation_warnings(payload: dict, response: dict, prompt_chars: int) -> list[str]:
    """Spot silently cut-off input or output (findings 5.4)."""
    warnings = []
    if response.get("done_reason") == "length":
        warnings.append("The answer was cut off (it hit the maximum answer length).")
    used = response.get("prompt_eval_count")
    num_ctx = (payload.get("options") or {}).get("num_ctx")
    if isinstance(used, int) and num_ctx and used >= num_ctx * 0.97:
        warnings.append("The input filled the whole context, so part of it was probably cut off. Try a larger context size.")
        return warnings
    # Ollama 0.40 reports the full prompt even when it reuses its cache (checked on Thor,
    # Nov 23 2026), so a count far below the estimate means the input was cut off.
    expected = prompt_chars / 4  # conservative: logs/prose run ~3.5–4 characters per token
    if isinstance(used, int) and used and expected > 200 and used < expected * 0.5:
        warnings.append(
            f"Ollama only read about {used} of an expected ~{int(expected)} prompt tokens; "
            "the input may have been cut off. Try a larger context size."
        )
    return warnings


async def discover(extra: list[str] | None = None, timeout: float = 2.0) -> list[dict]:
    """Try the likely addresses at once; return the ones that are Ollama."""
    candidates = list(dict.fromkeys((extra or []) + DISCOVERY_CANDIDATES))

    async def probe(url: str):
        client = OllamaClient(url, timeout=timeout)
        try:
            version = await client.version(timeout)
            fingerprint = tuple(inventory_fingerprint(await client.tags()))
        except OllamaError:
            return None
        return {"url": url, "version": version, "_fp": fingerprint}

    found = [f for f in await asyncio.gather(*(probe(u) for u in candidates)) if f]
    # The same Ollama often answers on several names (host.docker.internal and the gateway IP);
    # show it once. Servers with no models can't be told apart, so those are all kept.
    seen, unique = set(), []
    for f in found:
        fp = f.pop("_fp")
        if fp and fp in seen:
            continue
        seen.add(fp)
        unique.append(f)
    return unique
