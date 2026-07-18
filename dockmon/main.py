"""Dockmon entry point — runs startup checks then the monitor loop."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from dockmon.config import load_config, DockmonConfig
from dockmon.db import init_db, verify_tables
from dockmon.docker_client import get_client, get_logs, list_containers
from dockmon.log_pipeline import process_logs
from dockmon.ai_engine import evaluate, EvaluationContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dockmon")

# Graceful shutdown
_shutdown = asyncio.Event()


def _handle_signal(sig, frame):
    logger.info("Received %s, shutting down...", signal.Signals(sig).name)
    _shutdown.set()


def startup_check(cfg: DockmonConfig) -> None:
    """Run all Phase 0 checks: config, database, Docker connection."""
    print("=" * 50)
    print("  Dockmon — AI Container Monitor")
    print("=" * 50)
    print()

    # 1. Config
    enabled = [c for c in cfg.containers if c.enabled]
    logger.info("Config loaded: %d container(s) to monitor", len(enabled))
    for c in enabled:
        logger.info("  • %s", c.name)

    # 2. Database
    conn = init_db(cfg.monitoring.db_path)
    tables = verify_tables(conn)
    logger.info("Database OK: %s", tables)
    conn.close()

    # 3. Docker
    client = get_client()
    version = client.version()["Version"]
    containers = client.containers.list()
    logger.info("Docker %s: %d running container(s)", version, len(containers))

    running_names = {c.name for c in containers}
    for c in enabled:
        status = "FOUND" if c.name in running_names else "NOT FOUND"
        logger.info("  • %s: %s", c.name, status)

    logger.info("Mode: %s", "DRY RUN" if cfg.monitoring.dry_run else "LIVE")
    logger.info("Poll interval: %ds", cfg.monitoring.poll_interval_seconds)
    logger.info("Ollama: %s (%s)", cfg.ollama.base_url, cfg.ollama.default_model)


async def monitor_cycle(cfg: DockmonConfig) -> None:
    """Run one full monitoring cycle across all enabled containers."""
    client = get_client()
    enabled = [c for c in cfg.containers if c.enabled]
    running = list_containers(client, [c.name for c in enabled])
    running_map = {c.name: c for c in running}

    conn = init_db(cfg.monitoring.db_path)

    for container_cfg in enabled:
        if container_cfg.name not in running_map:
            logger.warning("Container %s not running, skipping", container_cfg.name)
            continue

        container = running_map[container_cfg.name]

        # 1. Grab logs
        raw_logs = get_logs(container, tail=cfg.monitoring.log_lines_per_check)

        # 2. Filter
        batch = process_logs(
            container_name=container_cfg.name,
            raw_logs=raw_logs,
            ignore_patterns=container_cfg.ignore_patterns,
            max_lines=cfg.monitoring.log_lines_per_check,
        )

        logger.info(
            "[%s] %d total lines → %d forwarded (dropped: %d ignore, %d info-level)",
            container_cfg.name,
            batch.total_lines,
            len(batch.filtered_lines),
            batch.dropped_by_ignore,
            batch.dropped_by_level,
        )

        # 3. Evaluate with AI
        model = container_cfg.model_override or cfg.ollama.default_model

        # Check for baseline
        baseline = None
        row = conn.execute(
            "SELECT healthy_log_sample FROM baselines WHERE container = ?",
            (container_cfg.name,),
        ).fetchone()
        if row:
            baseline = row[0]

        ctx = EvaluationContext(
            container_name=container_cfg.name,
            filtered_lines=batch.filtered_lines,
            model=model,
            baseline_sample=baseline,
        )

        result, prompt_version = await evaluate(ctx, cfg.ollama)

        # 4. Log the result
        logger.info(
            "[%s] → %s (confidence=%d, category=%s, action=%s): %s",
            container_cfg.name,
            result.status.upper(),
            result.confidence,
            result.root_cause_category,
            result.recommended_action,
            result.summary,
        )

        # 5. Store event
        conn.execute(
            """INSERT INTO events
               (container, event_type, ai_status, confidence, root_cause_category,
                summary, action_taken, log_snapshot, prompt_version, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                container_cfg.name,
                "evaluation",
                result.status,
                result.confidence,
                result.root_cause_category,
                result.summary,
                result.recommended_action,
                "\n".join(batch.filtered_lines[:50]),  # Store first 50 lines
                prompt_version,
                model,
            ),
        )
        conn.commit()

        # 6. Capture baseline if first healthy evaluation
        if result.status == "healthy" and result.confidence >= 80 and baseline is None:
            # Use unfiltered recent logs for baseline (grab fresh)
            baseline_logs = get_logs(container, tail=30)
            conn.execute(
                """INSERT OR REPLACE INTO baselines (container, healthy_log_sample)
                   VALUES (?, ?)""",
                (container_cfg.name, baseline_logs[:2000]),
            )
            conn.commit()
            logger.info("[%s] Baseline captured", container_cfg.name)

    conn.close()


async def run(cfg: DockmonConfig) -> None:
    """Main loop: run monitor cycles until shutdown."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    startup_check(cfg)
    logger.info("Starting monitor loop...")

    while not _shutdown.is_set():
        try:
            await monitor_cycle(cfg)
        except Exception:
            logger.exception("Error in monitor cycle")

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=cfg.monitoring.poll_interval_seconds)
            break  # Shutdown requested
        except asyncio.TimeoutError:
            pass  # Normal — timeout means it's time for the next cycle

    logger.info("Dockmon stopped.")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config/config.yaml"
    cfg = load_config(config_path)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
