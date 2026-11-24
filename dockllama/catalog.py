"""Best-guess model choice and download suggestions (plan section 8A).

DockLlama only *suggests*; the user always chooses. Real rankings come from testing on the
user's own server (phase 3b); until a model is tested, its catalog entry is the guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ollama import ModelDetails

GIB = 2**30
OVERHEAD_GIB = 1.0  # context cache + runtime at DockLlama's 8K context

# Memory DockLlama can count on for each answer to "what does the Ollama machine have?"
HARDWARE = {
    "none": {"label": "No graphics card", "fit": "runs on your CPU", "gib": 8, "cpu": True},
    "gpu4": {"label": "4–6 GB graphics card", "fit": "fits your 4–6 GB graphics card", "gib": 4.5, "cpu": False},
    "gpu8": {"label": "8 GB graphics card", "fit": "fits your 8 GB graphics card", "gib": 8, "cpu": False},
    "gpu12": {"label": "12 GB graphics card", "fit": "fits your 12 GB graphics card", "gib": 12, "cpu": False},
    "gpu16": {"label": "16 GB graphics card", "fit": "fits your 16 GB graphics card", "gib": 16, "cpu": False},
    "gpu24": {"label": "24 GB or more", "fit": "fits your graphics card", "gib": 24, "cpu": False},
    "apple": {"label": "Apple Silicon (shared memory)", "fit": "fits your Mac", "gib": 12, "cpu": True},
    "unknown": {"label": "Not sure", "fit": "should fit most computers", "gib": 8, "cpu": True},
}


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads((Path(__file__).parent / "models_catalog.json").read_text())


def catalog_entry(name: str) -> dict | None:
    return next((m for m in catalog()["models"] if m["name"] == name), None)


def _gib(size_bytes: int) -> float:
    return size_bytes / GIB


def fits(size_gib: float, hardware: str) -> bool:
    return size_gib + OVERHEAD_GIB <= HARDWARE.get(hardware, HARDWARE["unknown"])["gib"]


def _params_billions(text: str) -> float | None:
    m = re.match(r"([\d.]+)\s*([BM])", text or "", re.I)
    if not m:
        return None
    value = float(m.group(1))
    return value if m.group(2).upper() == "B" else value / 1000


@dataclass
class Suggestion:
    name: str
    reason: str


def best_guess(models: list[ModelDetails], hardware: str, loaded: set[str] | None = None) -> Suggestion | None:
    """Pick the installed model to suggest, with a one-line reason. None if nothing is usable."""
    loaded = loaded or set()
    usable = [m for m in models if m.usable]
    if not usable:
        return None
    hw = HARDWARE.get(hardware, HARDWARE["unknown"])
    fitting = [m for m in usable if fits(_gib(m.size), hardware)]
    pool = fitting or usable

    def rank(m: ModelDetails):
        entry = catalog_entry(m.name)
        if entry:
            fastest = min(entry["seconds"].values()) if entry["seconds"] else 99
            cpu_pref = 0 if (not hw["cpu"] or entry["type"] == "moe" or entry["cpu_ok"]) else 1
            return (0, cpu_pref, -entry["score"], fastest)
        params = _params_billions(m.parameter_size) or 0
        return (1, 0, abs(params - 8), _gib(m.size))  # untested: closest to ~8B parameters

    ranked = sorted(pool, key=rank)
    top = ranked[0]
    top_entry = catalog_entry(top.name)
    # Prefer a model that's already loaded on the server if it's (nearly) as good: no swapping.
    for m in ranked[1:]:
        e = catalog_entry(m.name)
        if m.name in loaded and e and top_entry and e["score"] >= top_entry["score"] - 2:
            return Suggestion(m.name, f"Tested well ({e['score']:.1f}/100) and already loaded on this server, so no model swapping.")
    if top.name in loaded and top_entry:
        return Suggestion(top.name, f"Tested well ({top_entry['score']:.1f}/100) and already loaded on this server.")
    if top_entry:
        where = hw["fit"] if fitting else "the best tested option you have, though it may run partly on the CPU"
        return Suggestion(top.name, f"Tested well ({top_entry['score']:.1f}/100) and {where}.")
    if fitting:
        return Suggestion(top.name, "Not tested by DockLlama yet, but it fits your hardware. Run a test to see how it does.")
    return Suggestion(top.name, "Nothing installed fits your hardware well; this is the closest. Consider a suggested download.")


def download_suggestions(models: list[ModelDetails], hardware: str, limit: int = 3) -> list[dict]:
    """Catalog models that fit the hardware and aren't installed, best first."""
    installed = {m.name for m in models}
    hw = HARDWARE.get(hardware, HARDWARE["unknown"])
    picks = []
    for entry in catalog()["models"]:
        if entry["name"] in installed:
            continue
        size_gib = entry["size_gb"] * 1e9 / GIB
        if not fits(size_gib, hardware):
            continue
        if hw["cpu"] and not (entry["cpu_ok"] or entry["type"] == "moe"):
            continue
        picks.append(entry)
    picks.sort(key=lambda e: (-e["score"], min(e["seconds"].values()) if e["seconds"] else 99))
    return [
        {"name": e["name"], "size_gb": e["size_gb"], "score": e["score"], "notes": e["notes"],
         "reference_seconds": e["seconds"]}
        for e in picks[:limit]
    ]
