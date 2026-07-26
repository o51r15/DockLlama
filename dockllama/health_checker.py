"""Background HTTP health check poller for DockLlama."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from dockllama.config import DockLlamaConfig
from dockllama.db import (
    init_db, get_enabled_health_checks, save_health_check_result, get_health_check_history,
)

logger = logging.getLogger("dockllama.healthcheck")

# In-memory state: {container: {"consecutive_failures": int, "last_check": float, "status": str}}
_hc_state: dict[str, dict] = {}

# Shutdown event for graceful stop
_shutdown_event = asyncio.Event()

# Reusable httpx client (created once, reused for all checks)
_http_client: httpx.AsyncClient | None = None


def get_hc_status(container: str) -> dict:
    """Get current health check status for a container (for dashboard)."""
    return _hc_state.get(container, {"status": "unknown", "consecutive_failures": 0})


def get_all_hc_statuses() -> dict[str, dict]:
    """Get all health check statuses."""
    return dict(_hc_state)


def request_shutdown() -> None:
    """Signal the health check poller to stop."""
    _shutdown_event.set()


async def _get_client(timeout: int = 10) -> httpx.AsyncClient:
    """Get or create the shared httpx client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    return _http_client


async def _check_one(hc: dict, cfg: DockLlamaConfig, conn) -> None:
    """Run a single health check and record the result."""
    container = hc["container"]
    url = hc["url"]
    method = hc["method"]
    expected = hc["expected_status"]
    timeout = hc["timeout_seconds"]
    threshold = hc["failure_threshold"]

    status_code = None
    response_ms = 0
    success = False
    error = None

    t0 = time.monotonic()
    try:
        client = await _get_client(timeout)
        r = await client.request(method, url, timeout=timeout)
        status_code = r.status_code
        response_ms = round((time.monotonic() - t0) * 1000)
        success = status_code == expected
        if not success:
            error = f"Expected {expected}, got {status_code}"
    except httpx.TimeoutException:
        response_ms = round((time.monotonic() - t0) * 1000)
        error = f"Timeout after {timeout}s"
    except Exception as e:
        response_ms = round((time.monotonic() - t0) * 1000)
        error = str(e)[:200]

    # Update in-memory state
    state = _hc_state.setdefault(container, {"status": "unknown", "consecutive_failures": 0})
    if success:
        state["consecutive_failures"] = 0
        state["status"] = "healthy"
    else:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= threshold:
            state["status"] = "failing"
        else:
            state["status"] = "degraded"

    state["last_check"] = time.monotonic()
    state["last_status_code"] = status_code
    state["last_response_ms"] = response_ms
    state["last_error"] = error

    # Save to DB (reuse the passed-in connection)
    try:
        save_health_check_result(conn, container, status_code, response_ms, success, error)
    except Exception as e:
        logger.debug("Failed to save HC result for %s: %s", container, e)

    if success:
        logger.debug("[HC] %s: %d %dms OK", container, status_code, response_ms)
    else:
        logger.warning("[HC] %s: %s (%d consecutive failures)",
                       container, error, state["consecutive_failures"])

    # Alert on threshold breach
    if state["consecutive_failures"] == threshold:
        logger.error("[HC] %s: FAILED %d consecutive checks — threshold reached", container, threshold)
        try:
            from dockllama.alerts import alert_error
            from dockllama.db import is_blackout_active as _is_bo
            if not _is_bo(conn):
                alert_error(f"Health check failed for {container}: {threshold} consecutive failures — {error}")
            else:
                logger.info("[HC] %s: threshold reached but blackout active — alert suppressed", container)
        except Exception:
            pass


async def run_health_checks(cfg: DockLlamaConfig) -> None:
    """Background loop: poll all enabled health checks at their configured intervals."""
    logger.info("Health check poller started")

    # Single DB connection for the poller lifetime
    conn = init_db(cfg.monitoring.db_path)

    try:
        while not _shutdown_event.is_set():
            try:
                checks = get_enabled_health_checks(conn)
            except Exception:
                # DB connection might be stale; reconnect
                try:
                    conn.close()
                except Exception:
                    pass
                conn = init_db(cfg.monitoring.db_path)
                checks = get_enabled_health_checks(conn)

            if not checks:
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=30)
                    break  # shutdown requested
                except asyncio.TimeoutError:
                    continue

            now = time.monotonic()
            tasks = []
            for hc in checks:
                container = hc["container"]
                state = _hc_state.get(container, {})
                last = state.get("last_check", 0)
                if (now - last) >= hc["interval_seconds"]:
                    tasks.append(_check_one(hc, cfg, conn))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Sleep until the next check is due (min 5s to avoid tight loops)
            if checks:
                min_interval = min(hc["interval_seconds"] for hc in checks)
                sleep_time = max(5, min_interval / 2)
            else:
                sleep_time = 30

            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=sleep_time)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal — just means it's time to check again
    finally:
        conn.close()
        if _http_client and not _http_client.is_closed:
            await _http_client.aclose()
        logger.info("Health check poller stopped")
