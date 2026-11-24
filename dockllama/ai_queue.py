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

checks_paused = False
_locks: dict[int, asyncio.Lock] = {}


def lock() -> asyncio.Lock:
    """The shared lock for the running event loop (tests start a new loop per app)."""
    loop = asyncio.get_running_loop()
    if id(loop) not in _locks:
        _locks.clear()  # only one loop runs the app at a time
        _locks[id(loop)] = asyncio.Lock()
    return _locks[id(loop)]
