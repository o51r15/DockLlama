"""Who gets to use the AI server right now.

Ollama answers one big request at a time well; two at once just makes both slow (and can
force a model swap). So container checks (phase 5) and model tests share one lock:

- a check round holds the lock for the round;
- a model test holds it for one model's test (its 8 scenarios), then releases it, so a
  waiting check round goes next. Checks are never delayed by more than one model's test.

asyncio.Lock is first-come, first-served, which gives exactly that alternation.

"Test all now (wait and watch)" pauses checks instead: `checks_paused` is set while it runs,
and the check engine skips rounds while it's set.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

checks_paused = False
_locks: dict[int, asyncio.Lock] = {}


def lock() -> asyncio.Lock:
    """The shared lock for the running event loop (tests start a new loop per app)."""
    loop = asyncio.get_running_loop()
    if id(loop) not in _locks:
        _locks.clear()  # only one loop runs the app at a time
        _locks[id(loop)] = asyncio.Lock()
    return _locks[id(loop)]


# --- who is in line (for "Check now: waiting in line, 2 ahead") ------------------------------

class _Turn:
    __slots__ = ("kind", "label", "container_id", "holding")

    def __init__(self, kind: str, label: str, container_id: int | None):
        self.kind, self.label, self.container_id, self.holding = kind, label, container_id, False


line: list[_Turn] = []  # in arrival order; the one holding the lock is marked


@asynccontextmanager
async def turn(kind: str, label: str = "", container_id: int | None = None):
    """Wait for the shared lock like `async with lock()`, but visibly: kind is round | check | test | summary."""
    t = _Turn(kind, label, container_id)
    line.append(t)
    try:
        async with lock():
            t.holding = True
            yield
    finally:
        if t in line:
            line.remove(t)


def position(container_id: int, round_left: int) -> dict | None:
    """How many things are ahead of this container's Check now, or None if it isn't waiting.
    A running check round counts as the containers it still has to do."""
    mine = next((t for t in line if t.kind == "check" and t.container_id == container_id and not t.holding), None)
    if mine is None:
        return None
    ahead, what = 0, []
    for t in line:
        if t is mine:
            break
        if t.kind == "round":
            n = max(1, round_left) if t.holding else 1
            ahead += n
            what.append(f"the check round ({n} container{'s' if n != 1 else ''} left)" if t.holding else "a check round")
        elif t.kind == "test":
            ahead += 1
            what.append(f"a model test ({t.label})" if t.label else "a model test")
        elif t.kind == "summary":
            ahead += 1
            what.append("the daily summary")
        else:
            ahead += 1
            what.append("another Check now")
    return {"ahead": ahead, "what": what}
