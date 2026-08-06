# DockLlama — Developer Log & Handoff

**Last updated:** July 25, 2026 (Session 8)
**Repository:** https://github.com/o51r15/DockLlama (renamed from DockLlama)
**Status:** Running in dry-run mode as Docker container, monitoring 20 production containers, 5 HTTP health checks configured
**Latest commit:** `f93c5e8` — Update develop log: Session 8 final

## FIRST TASK: Complete Rename ✅ DONE (Session 4)
Renamed dockmon → dockllama across 32 files including Python package dir, imports, Docker image, CI/CD, compose, all user-facing strings.

---

## Quick Start for New Chat Sessions

When the user says **"lets start the roadmap"**:

1. Read `roadmap.md` (in the same directory as this file)
2. Find the phase marked **⬅️ START HERE**
3. Work through it sub-phase by sub-phase using the workflow below
4. After completing a phase, move the **⬅️ START HERE** marker to the next phase

**Development workflow for EVERY change — no exceptions:**

1. **Inspect** — Read the source files you're about to modify. Understand what exists.
2. **Build** — Write the code change. For complex edits, write a Python patch script, pscp it to `/tmp/` on the server, execute remotely.
3. **Review** — Syntax check with `ast.parse()`. Read the modified file back. Verify the diff is what you intended.
4. **Test** — Commit, push, rebuild container, restart, check logs, hit the API, verify the change works end-to-end.
5. **Review** — Confirm test results. Check for regressions. Fix before moving on.
6. **Move to next** — Update the roadmap status, then start the next sub-phase.

**One sub-phase = one commit = one test cycle.** Do NOT batch multiple sub-phases.

---

## What Is Dockmon

Dockmon is an AI-driven Docker container health monitoring system. It polls container logs, runs them through a structured Python preprocessor, sends summaries to a local LLM (Ollama), and takes action (restart, alert, or dry-run log) based on the AI's health assessment. It has a web dashboard, daily digest reports, and Apprise-based notifications.

**Stack:** Python 3.14, FastAPI, Alpine.js, SQLite (WAL), Ollama (llama3.1:8b default, gemma4 digest)

---

## Infrastructure

| Component | Location | Details |
|-----------|----------|---------|
| **Dockmon code** | `/home/o51r15/scripts/dockmon/` on Optiplex server | Python 3.14, runs as Docker container |
| **Optiplex server** | `192.168.1.192` | Runs Docker containers + Dockmon |
| **GPU server** | `192.168.1.125` | Runs Ollama (LLM inference) |
| **Ollama API** | `http://192.168.1.125:11434` | llama3.1:8b (eval), gemma4:latest (digest) |
| **Dockmon Web UI** | `http://192.168.1.192:8556` | FastAPI + static HTML |
| **GitHub repo** | `ghcr.io/o51r15/dockmon` | Docker images via GitHub Actions |
| **SSH access** | PuTTY (plink/pscp) via Desktop Commander MCP | **NEVER use bash sandbox for SSH** |

### SSH Commands (CRITICAL)

The `mcp__workspace__bash` tool runs in an isolated Linux sandbox — it CANNOT reach the server. All server operations MUST use Desktop Commander MCP tools (load via ToolSearch: `select:mcp__Desktop_Commander__start_process`):

```
# Run command on server
C:\PROGRA~1\PuTTY\plink.exe -batch -i C:\Users\o51r15\.ssh\id_ed25519.ppk o51r15@192.168.1.192 "<command>"

# Copy file TO server
C:\PROGRA~1\PuTTY\pscp.exe -batch -i C:\Users\o51r15\.ssh\id_ed25519.ppk <local_file> o51r15@192.168.1.192:<remote_path>

# Copy file FROM server
C:\PROGRA~1\PuTTY\pscp.exe -batch -i C:\Users\o51r15\.ssh\id_ed25519.ppk o51r15@192.168.1.192:<remote_path> <local_file>
```

**PowerShell gotcha:** Special characters (`$`, single quotes inside double quotes, Python one-liners) get mangled by PowerShell. For complex commands, write a Python script locally, pscp it to `/tmp/` on the server, then execute it remotely with `python3 /tmp/script.py`.

### Docker Container Management

```bash
# Full rebuild and restart cycle (run on server via plink)
cd /home/o51r15/scripts/dockmon
docker build -t ghcr.io/o51r15/dockllama:dev .
docker stop dockllama && docker rm dockllama
docker run -d --name dockllama --restart unless-stopped \
  -p 8556:8556 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /home/o51r15/docker/dockmon/config.yaml:/app/config/config.yaml \
  -v dockmon-data:/app/data \
  -e TZ=America/New_York \
  ghcr.io/o51r15/dockllama:dev

# Check logs
docker logs dockllama --tail 20

# Alternative: run from host (for debugging only)
cd ~/scripts/dockmon && nohup python3 -m dockllama config.yaml > /tmp/dockmon.log 2>&1 &
fuser -k 8556/tcp  # to stop host mode
```

### Git Workflow

All git operations run from `/home/o51r15/scripts/dockmon/` on the server:

```bash
git add <files>
git commit -m "descriptive message"
git push origin main
```

**config.yaml is gitignored.** Never push it. `config.example.yaml` is the git template.

---

## Project Structure

```
dockmon/
├── dockmon/
│   ├── __init__.py
│   ├── __main__.py          # Entry point (calls main.main())
│   ├── main.py              # Monitor loop, web server, digest scheduler, startup checks
│   ├── config.py            # Pydantic config schema + YAML loader
│   ├── db.py                # SQLite schema (events, cooldowns, baselines, digests, alert_urls)
│   ├── docker_client.py     # Docker SDK wrapper (get_client, get_logs, list_containers)
│   ├── log_pipeline.py      # Legacy pre-filter (ANSI strip, level detect, ignore patterns)
│   ├── log_analyzer.py      # v5 structured preprocessor (LogSummary with .to_prompt())
│   ├── ai_engine.py         # Ollama LLM evaluation (EvaluationContext → EvaluationResult)
│   ├── actions.py           # Restart/dry-run logic + cooldown system + compose group restarts
│   ├── alerts.py            # Apprise notification layer + DB persistence (load_alert_urls, save_alert_urls)
│   ├── digest.py            # Daily digest generation (gemma4) + DB storage
│   ├── health_checker.py    # Background HTTP health check poller
│   ├── trends.py            # 7d/30d health trend calculations
│   ├── prompts/
│   │   ├── v5_evaluate.txt  # Current eval prompt (structured input from log_analyzer)
│   │   └── v1_digest.txt    # Digest summary prompt
│   └── api/
│       ├── routes.py        # FastAPI REST endpoints (containers, logs, evaluate, events, alerts, digests, config, health)
│       └── events.py        # SSE event stream (publish/subscribe)
├── frontend/
│   ├── index.html           # Dashboard (container health grid only)
│   ├── insights.html        # Insights hub (sidebar: Health Trends, Recent Events, Log Explorer, Digest)
│   ├── settings.html        # Settings hub (sidebar: Notifications, Prompts)
│   ├── explorer.html        # Redirect → /insights.html?view=explorer
│   ├── digest.html          # Redirect → /insights.html?view=digest
│   └── prompts.html         # Redirect → /settings.html?view=prompts
├── config.yaml              # LOCAL config (gitignored) — 15 containers, real settings
├── config.example.yaml      # Generic template (in git)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .github/workflows/docker-publish.yml
└── .gitignore
```

---

## Evaluation Pipeline (How It Works)

Understanding this pipeline is critical for implementing Phases 7 and 8:

```
Raw Docker logs (200 lines)
    ↓
log_analyzer.py: analyze_logs()
    → Strips ANSI, Docker timestamps
    → Applies ignore_patterns (regex filter)
    → Counts severities (INFO/WARN/ERROR)
    → Detects restart sequences (shutdown → startup)
    → Deduplicates error messages with counts
    → Detects recovery (errors followed by clean lines)
    → Extracts recent tail (last 25 unfiltered lines)
    → Returns LogSummary with .to_prompt()
    ↓
ai_engine.py: evaluate()
    → Builds system prompt from v5_evaluate.txt
    → User prompt = LogSummary.to_prompt() output
    → Sends to Ollama (llama3.1:8b, format=json)
    → Parses EvaluationResult (status, health_score, confidence, etc.)
    ↓
main.py: _process_container()
    → Logs result
    → Stores in SQLite events table
    → Publishes via SSE
    → Executes action (restart/dry-run/none) based on result
    → Sends Apprise alerts
```

**Key files for Phase 7 (telemetry):** `docker_client.py` (add stats fetching), `log_analyzer.py` (add metrics to LogSummary), `v5_evaluate.txt` (add correlation rules)

**Key files for Phase 8 (context injection):** `config.py` (add context_prompt/examples/known_patterns to ContainerConfig), `ai_engine.py` (append context to system prompt), `log_analyzer.py` (metadata tagging), `main.py` (pass new fields through)

---

## Configuration

**config.yaml** (gitignored, on server): 15 containers, `poll_interval_seconds: 412`, `timeout_seconds: 300`, `base_url: "http://192.168.1.125:11434"`, `default_model: "llama3.1:8b"` (was briefly qwen2.5:7b-instruct, reverted) (was briefly qwen2.5:7b-instruct, reverted), `digest_model: "gemma4:latest"`, `dry_run: true`.

**Monitored containers (20):** gluetun, bitmagnet, bitmagnet-postgres, qbittorrent, kometa, audiobookshelf, pinchfork, pinchfork-db, jellyfin, karakeep, karakeep_chrome, karakeep_meilisearch, memos, tautulli, seerr

**Health-checked containers (5):** jellyfin, tautulli, seerr, audiobookshelf, memos (via HTTP endpoint polling)

**Compose groups** (restart together): bitmagnet (bitmagnet + bitmagnet-postgres), pinchfork (pinchfork + pinchfork-db), karakeep (karakeep + karakeep_chrome + karakeep_meilisearch)

**Docker volume:** `dockmon-data:/app/data` persists the SQLite DB across container restarts.

---

## Database Schema

```sql
events (id, container, timestamp, event_type, ai_status, confidence,
        root_cause_category, summary, action_taken, log_snapshot,
        prompt_version, model_used, health_score)

cooldowns (container PK, last_restart, consecutive_restarts,
           current_cooldown_minutes, alert_only_mode)

baselines (container PK, healthy_log_sample, captured_at)

digests (id, date, generated_at, overall_health, headline,
         digest_json, formatted_text)

alert_urls (id, url UNIQUE, added_at)

container_prompts (container TEXT PK, context_prompt TEXT, examples TEXT,
                   known_patterns TEXT, updated_at TEXT)

health_checks (container TEXT PK, url TEXT, method TEXT, expected_status INT,
               interval_seconds INT, timeout_seconds INT, failure_threshold INT,
               enabled INT)

blackout_windows (id INT PK, name TEXT, days TEXT JSON, start_time TEXT,
                  end_time TEXT, enabled INT)

health_check_results (id INT PK, container TEXT, timestamp TEXT,
                      status_code INT, response_ms INT, success INT, error TEXT)
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/containers | List all monitored containers + latest eval |
| GET | /api/containers/{name}/logs | Fetch container logs (raw + filtered) |
| POST | /api/containers/{name}/evaluate | On-demand AI evaluation |
| GET | /api/events | Paginated event history |
| GET | /api/events/{container}/restarts | Restart history for a container |
| GET | /api/trends | 7d/30d health trends |
| GET | /api/config | Current running config (sanitized) |
| GET | /api/health | System health check (Docker, Ollama, DB) |
| POST | /api/digest | Trigger on-demand digest |
| GET | /api/alerts | Get alert URLs (from DB) |
| PUT | /api/alerts | Update alert URLs (persisted to DB) |
| POST | /api/alerts/test | Send test notification |
| GET | /api/digests | List stored digests |
| GET | /api/digests/latest | Most recent digest |
| GET | /api/digests/{date} | Digest by date (YYYY-MM-DD) |
| GET | /api/containers/{name}/prompt | Get effective prompt config (DB + config fallback) |
| PUT | /api/containers/{name}/prompt | Save prompt config to DB |
| DELETE | /api/containers/{name}/prompt | Revert to config.yaml defaults |
| POST | /api/containers/{name}/test-prompt | Test prompt against current logs without saving |
| GET | /api/containers/{name}/healthcheck | Get health check config |
| PUT | /api/containers/{name}/healthcheck | Configure health check |
| DELETE | /api/containers/{name}/healthcheck | Remove health check |
| GET | /api/containers/{name}/healthcheck/history | Health check result history |
| GET | /api/healthchecks | List all health checks with last result |
| GET | /api/blackouts | List all blackout windows |
| POST | /api/blackouts | Create a blackout window |
| PUT | /api/blackouts/{id} | Update a blackout window |
| DELETE | /api/blackouts/{id} | Delete a blackout window |
| GET | /api/blackouts/active | Check if blackout is currently active |

---

## Key Architecture Decisions

### v5 Structured Preprocessor Pipeline

The biggest architectural win. Instead of dumping raw filtered logs to the LLM:

1. `log_analyzer.py` does all mechanical analysis in Python: timestamp parsing, severity counting, error deduplication, recovery detection, restart detection
2. Produces a `LogSummary` object with `.to_prompt()` method
3. LLM receives a structured summary (~20 lines) instead of raw logs (~200 lines)
4. Result: even 3B models get correct health assessments; 8B model is the sweet spot

### Auto-Healthy Fast Path

When ALL log lines match ignore patterns → `total_lines == 0` → skip LLM entirely → return synthetic healthy result (score=95). Prevents hallucination on empty input and saves compute.

### Model Selection

| Model | Size | Time | Use |
|-------|------|------|-----|
| llama3.1:8b | 4.7GB | ~11s | **Default** eval model |
| gemma4:latest | — | ~15s | Digest summaries (num_predict=4096) |

### Cold Model Problem

With poll intervals > 5 minutes, Ollama unloads the model from GPU. First eval hits cold model load (~30-40s). Fix: `timeout_seconds: 300` in config.

---

## Prompt Engineering History

Five versions, each fixing failure modes discovered live:

- **v1:** Basic 3-tier (healthy/unhealthy/critical). No root cause or restart reasoning.
- **v2:** Added error origin tracking, root cause categories, restart reasoning. Cut false restarts.
- **v3:** Shifted from "are there errors?" to "is the container doing its job?" PostgreSQL FATAL during planned shutdown = healthy.
- **v4:** Added "degraded" tier and 0–100 numeric score. Recency awareness with numbered log lines.
- **v5 (current):** Python preprocessor handles mechanical analysis; LLM only interprets structured summary. Fixed the fundamental pipeline flaw where filtered INFO lines removed recovery evidence.

---

## CI/CD

`.github/workflows/docker-publish.yml`: push to main → `ghcr.io/o51r15/dockllama:dev`, GitHub Release → `:latest` + semver. Currently running `:dev` built locally (GHCR pull had timeout issues).

---

## Development Timeline

### Session 1 — Initial Build (July 17–18, 2026)

Built entire project from scratch. Phases 0–5: scaffolding → log pipeline/AI engine → actions/cooldowns/alerts → FastAPI/SSE/dashboard → digest/scheduler → hardening. 8 containers. Prompt evolution v1–v4. Discovered the recency problem.

### Session 2 — Preprocessor & Scale (July 18, 2026)

Built v5 structured preprocessor (biggest quality improvement). Switched to llama3.1:8b. Added GitHub Actions CI/CD. Expanded to 15 containers. Separated config.

### Session 3 — Features & Fixes (July 19–23, 2026)

Auto-healthy fast path. Expanded Chrome ignore patterns. Settings page + Digest viewer. Fixed digest generation (num_predict too low for 15 containers + robust JSON extraction). Persisted notification URLs in SQLite. Updated roadmap with Phases 7–11 including detailed telemetry and context injection plans.

Key commits this session: `d3b4c52` (auto-healthy, digest fix, settings/digest pages), `fce7554` (digest num_predict bump), `6bee8b0` (alert URL persistence fix).
### Session 4 — Telemetry, Context Injection & Model Tuning (July 23, 2026)

**Rename:** Completed dockmon → dockllama across 32 files (package dir, imports, Docker, CI/CD, compose, strings).

**Phase 7 (Telemetry):** Added `get_container_stats()` to docker_client.py using Docker stats API with delta CPU calculation. Added cpu_percent/mem_percent to LogSummary dataclass and to_prompt() output. Added resource correlation rules to v5_evaluate.txt. All 15 containers now report CPU/RAM metrics.

**Phase 8.1-8.3 (Context Injection):** Added context_prompt, examples, and known_patterns fields to ContainerConfig. context_prompt injects container-specific knowledge into the system prompt. Few-shot examples provide calibration scenarios. known_patterns tag matching log lines with [ROUTINE:] metadata. Configured context for gluetun (port forwarding failures), bitmagnet-postgres (shutdown sequences), and karakeep_chrome (headless Chromium noise).

**Base prompt improvements:** Added "What you are reading" section to v5_evaluate.txt explaining the full preprocessing pipeline (ignore_patterns, known_patterns, severity counting, context overrides). Added "Reading the severity line" section explaining routine counts.

**Routine counts:** Added routine_counts tracking to log_analyzer.py — counts how many ERROR/WARN lines carry [ROUTINE:] tags and displays inline: `18 (18 routine) ERROR`.

**Model switch:** Switched from qwen2.5:7b-instruct back to llama3.1:8b for better context-following.

**Known bug:** karakeep_chrome scores 60 (DEGRADED) despite all errors being routine Chromium noise. See BUG_karakeep_chrome_scoring.md for full analysis and future fix ideas. Best candidate: reclassify routine-tagged lines as INFO in the preprocessor.

Key commits: rename (32 files), Phase 7 metrics pipeline, Phase 8 context injection, preprocessing explanation in base prompt, routine counts in severity display.


### Session 5-6 — Model Testing, Stats History, Bug Fixes & Config Persistence (July 23, 2026)

**Phase 9.1-9.3 (Model Validation & Scheduling):** Model discovery UI queries Ollama for available models. Validation testing sends healthy/failing test fixtures with warmup call to avoid cold-start benchmark skew. Results persist in tested_models DB table with full results_json. Selectable model cards show stored test results and interval calc. Hardware-aware interval slider with red/yellow/green zones auto-jumps to recommended value.

**Phase 7B (Stats History & Resource Charts):** Container stats persist to container_stats table with configurable retention. API endpoints with downsampling for 1h/24h/7d/30d ranges. Dashboard sparklines on each card. Detail drawer with Chart.js line charts. Fleet overview bar.

**DB path fix:** Config had db_path pointing to host path inaccessible inside container. Fixed to /app/data/dockllama.db (volume-mounted). Migrated old data (3772 events, 16 baselines, 5 digests). Added warning comment to config.yaml.

**Evaluate Now bug fixes:** Button click bubbled to detail drawer (added @click.stop). On-demand eval now saves to events table as on_demand_eval event type so dashboard reflects new results.

**Config persistence:** Interval and model changes from UI now persist to config.yaml via targeted regex replacement (preserves comments/formatting). Removed :ro from config mount. Single source of truth.

Key commits: ad8df22, 851c8bc, 487f205, 98c4eb9, 7487933.

---



### Session 8 — Setup Wizard, Health Checks & Blackout Windows (July 25, 2026)

**Phase 9.5: First-Run Setup Wizard** (`1a116a6`): Added a guided multi-step setup wizard at `/setup.html` for new installations. The wizard walks users through:
1. **Ollama connection** — enter URL, test connectivity (with `PUT /api/setup/ollama-url` that validates and persists)
2. **Model selection** — list available models from Ollama, run validation tests (reuses Phase 9.2 test fixtures)
3. **Container selection** — show all Docker containers with checkboxes, add/remove from monitoring (reuses Phase 9.4 endpoints)
4. **Interval recommendation** — auto-calculate safe poll interval with color-coded slider (reuses Phase 9.3 calculator)

The dashboard (`index.html`) checks `GET /api/setup/status` on load — if `needs_setup` is true (no containers, no Ollama, or no validated model), it redirects to the wizard. For existing installations, the check returns false and the dashboard loads normally. The wizard is also accessible directly at `/setup.html` for reconfiguration.

All wizard steps reuse existing API endpoints — no duplicate backend logic. New endpoints added: `GET /api/setup/status` (setup state check) and `PUT /api/setup/ollama-url` (URL update with connectivity validation).

Key commit: 1a116a6.

**Phase 6.5: HTTP Health Checks** (`e00623f`, `d1f453e`): Added external HTTP endpoint monitoring as a complement to AI-driven log evaluation. Components:
- `health_checks` and `health_check_results` DB tables with full CRUD in db.py
- API endpoints: `GET/PUT/DELETE /api/containers/{name}/healthcheck`, `GET /api/containers/{name}/healthcheck/history`, `GET /api/healthchecks`
- Background async poller (`health_checker.py`) with in-memory state tracking (healthy/degraded/failing/unknown), configurable intervals, timeouts, and failure thresholds
- Settings page: full Health Checks management section with add/edit/delete forms and status badges
- Dashboard: health check status dot (green/yellow/red) on container cards alongside the AI health score dot
- Poller runs as additional task in `asyncio.gather()` alongside monitor, digest scheduler, and web server

Key commits: e00623f, d1f453e.

**Phase 6.6: Blackout Windows / Maintenance Schedules** (`318629d`, `bb3e7af`): Added the ability to schedule maintenance windows that suppress alerts and auto-restarts while still running AI evaluations (data collection continues). Components:
- `blackout_windows` DB table with CRUD functions and `is_blackout_active()` supporting overnight spans (e.g. 22:00-06:00) and weekday selection (0=Mon, 6=Sun)
- API endpoints: `GET/POST /api/blackouts`, `PUT/DELETE /api/blackouts/{id}`, `GET /api/blackouts/active`
- Blackout check in `_process_container()` — when active, skips action execution and alert sending but still runs evals and stores events
- Blackout check in health_checker.py — suppresses threshold-breach alerts during maintenance
- Settings page: Blackout Windows management section under Notifications with day checkboxes, time pickers, and enable/disable toggle
- Dashboard: yellow warning banner when a blackout window is active showing the window name and time range

Key commits: 318629d, bb3e7af.

### Session 7 — Code Audit, Bug Fixes, Dashboard Performance & Duplication Regression (July 24, 2026)

**Fresh-eyes code audit** of Session 5-6 work surfaced 6 issues (bugs #6-#11), all fixed in `c012c32`:
- #6 Ollama error-response handling with retry+backoff (GPU contention from the video transcoder made Ollama return `{"error": "Connection refused"}` at HTTP 200; the parser choked on the missing `response` key and returned garbage 50/0% verdicts, only for inspectarr since it happened to land during transcode).
- #7 regex backreference injection in `_update_yaml_field` (lambda replacement).
- #8 `save_containers_to_config` comment preservation (targeted text replacement).
- #9 `_config_path` → `PrivateAttr()`. #10 DELETE `?purge=true` query param. #11 dead-code cleanup.

**Dashboard performance overhaul** (`f7e71db`, `72e2c45`): eliminated multi-minute load times. Root cause was the eval loop blocking the shared asyncio event loop with synchronous Docker SDK calls, plus `/api/containers` making 19 Docker calls per request. Fixed by threading all blocking calls, starting the web server first, and caching the container snapshot. Dashboard now loads in 7-10ms even mid-eval.

**Duplication regression** (`8a72f4b`): the #8 comment-preserving rewrite duplicated the container list on every save (18 → 36) because it mistook YAML list items for top-level keys. Duplicate keys broke Alpine's card rendering entirely. De-duplicated the live config, restarted, and fixed the detection logic. Old config backed up to `/tmp/config.yaml.bak` on the server.

**README rewrite** (`d597415`): modern format modeled on popular self-hosted projects (Dozzle, Uptime Kuma) — centered header, badges, positioning vs log aggregators, full current feature list, Docker quickstart, complete API reference.

Key commits: c012c32, 9bec74f, d597415, f7e71db, 72e2c45, 8a72f4b.

---

## Lessons Learned

- **Never send empty data to an LLM.** If nothing to evaluate, use auto-healthy fast path.
- **Filter Chrome by source filename, not message text.** Source filenames are stable across versions.
- **Make every LLM JSON field optional with defaults.** llama3.1:8b occasionally omits fields.
- **Set num_predict high enough for output size.** gemma4 digest for 15 containers needs ~3000 tokens.
- **Account for cold model load time.** Set timeout_seconds: 300.
- **PowerShell mangles special chars.** Write Python scripts, pscp to server, execute remotely.
- **Separate local config from git.** config.yaml is gitignored.


---

## Project Review & Ideas 7-25-2026

Full code review of all backend Python files and frontend HTML/JS, plus live API timing tests.

### Bugs (code will break under specific conditions)

1. **`ai_engine.py`: Missing `import asyncio`** — `await asyncio.sleep(5)` is used in the Ollama error retry logic but `asyncio` is never imported. Will raise `NameError` on the first Ollama connection error or timeout. Silent until Ollama goes down.

2. **`actions.py`: Blocking `time.sleep()` in async context** — `time.sleep(10)` and `time.sleep(20)` in restart verification block the entire asyncio event loop for 30 seconds total. During a container restart, all other coroutines (web server, SSE, health checks, other evals) are frozen. Should use `await asyncio.sleep()`.

3. **`main.py`: `action` variable undefined when blackout active** — In `_process_container()`, when `blackout=True`, the code skips action execution (where `action` is assigned) but then checks `if not blackout and action.action_taken != "none"`. Safe only because Python short-circuits `and` — but if the condition is ever refactored (e.g., to `if action.action_taken != "none" and not blackout`), it raises `NameError`. Should initialize `action = None` before the blackout check.

4. **`index.html`: Blackout banner inside loading template** — The blackout warning banner (`<div class="blackout-banner">`) is nested inside `<template x-if="loading">`, so it only renders while the dashboard is loading and disappears once `loading = false`. The banner should be moved outside the loading template so it persists on the dashboard.

5. **`settings.html`: `loadingContainers` declared twice** — The Alpine component declares `loadingContainers: false` (for the docker containers section) and later `loadingContainers: true` (for the prompts section). The second declaration wins in JavaScript object literals, so the containers section may show a false "loading" state. Use separate variable names (e.g., `loadingDocker` and `loadingPromptContainers`).

6. **`settings.html`: Health checks view doesn't auto-load on direct URL** — `init()` checks for `?view=models` and `?view=containers` to auto-load data, but not `?view=healthchecks`. Navigating directly to `settings.html?view=healthchecks` shows an empty page until the user clicks away and back.

### Code Quality & Optimization

7. **`db.py`: Redundant local imports** — At least 6 functions contain `from datetime import datetime, timezone` and/or `import json` despite both being imported at module level. Adds unnecessary overhead and makes the code harder to follow.

8. **`db.py`: `if __name__ == "__main__"` test block in the middle of the file** — Around line 245, a test/debug block sits between function definitions. Should be moved to the end of the file.

9. **`db.py`: `get_all_health_checks()` only returns enabled checks** — The function uses `WHERE enabled = 1` despite the name suggesting it returns all checks. Either rename to `get_enabled_health_checks()` or add an optional parameter.

10. **`health_checker.py`: New DB connection per result save** — Each health check result opens and closes a new SQLite connection. With 5 checks at 60s intervals, that's 5 connections/minute. Should reuse a single connection or use a connection pool.

11. **`health_checker.py`: New `httpx.AsyncClient` per check** — A new HTTP client is created for every health check instead of reusing one. Loses connection pooling, TLS session reuse, and adds overhead.

12. **`health_checker.py`: No shutdown mechanism** — `run_health_checks()` uses `while True` with no way to gracefully stop (unlike the main monitor loop which has shutdown signaling). Could orphan the coroutine on shutdown.

13. **`main.py`: Separate `conn_stats` DB connection per container** — In the monitor loop, a new SQLite connection is opened and closed for each container just to save stats. Should batch or reuse.

14. **`main.py`: Split import of `is_blackout_active`** — Imported on a separate line from other `db` imports. Minor but should consolidate.

15. **`actions.py`: Creates new `docker.from_env()` for group restarts** — `execute_action()` receives a Docker client parameter but creates a brand-new one for compose/dependency group restarts. Should reuse the passed-in client.

### Frontend / UX Issues

16. **Monolithic `settings.html` (1265 lines)** — All 5+ settings views (Notifications, Prompts, Models, Health Checks, Containers, Blackout Windows) live in a single Alpine.js component with one massive `settingsApp()` function. Hard to maintain and debug. Consider splitting into separate components or at least separate JS files.

17. **No mobile responsiveness** — Sidebar navigation uses a fixed 200px width with no media queries. On mobile/tablet screens, the sidebar takes disproportionate space and content overflows. Dashboard card grid also has no mobile breakpoint.

18. **No offline/error indicator** — If the DockLlama server goes down, SSE silently reconnects every 5 seconds but the user sees stale data with no visual indication that the connection is lost. Should show a disconnected banner.

19. **No pagination on events table** — Insights → Recent Events is hardcoded to 20 events with no pagination, "load more," or date filtering. Users can't view historical events.

20. **CSS duplicated across all HTML files** — The same color variables, header, nav, sidebar, and button styles are copy-pasted into all 4 HTML files (~150 lines each). A shared CSS file would reduce maintenance burden and ensure consistency.

21. **Dashboard sparklines fire N parallel requests** — On load, `loadSparklines()` fires one `/api/containers/{name}/stats` request per container simultaneously (20 requests). Should batch or stagger.

22. **No favicon** — Browser tab shows the generic document icon. A simple llama SVG favicon would improve branding.

23. **SSE event handler reconnects on ANY error** — `eventSource.onerror` reconnects after 5s regardless of error type. No exponential backoff, no max retry limit, no user notification.

24. **No keyboard accessibility** — Sidebar navigation items use `<a>` tags with `@click` but no `href`, making them inaccessible to keyboard navigation and screen readers.

### API Performance (measured from host, July 25 2026)

| Endpoint | Response Time | Notes |
|---|---|---|
| `/api/config` | 2ms | Fast (file read) |
| `/api/blackouts/active` | 2ms | Fast |
| `/api/blackouts` | 2ms | Fast |
| `/api/alerts` | 2ms | Fast |
| `/api/events?limit=20` | 3ms | Fast |
| `/api/stats/fleet?range=1h` | 3ms | Fast |
| `/api/digests?limit=90` | 3ms | Fast |
| `/api/healthchecks` | 5ms | Fast |
| `/api/models` | 22ms | Hits Ollama API |
| `/api/containers` | 216ms | Docker SDK + cache |
| `/api/setup/status` | 254ms | Hits Docker + Ollama |
| `/api/trends` | 363ms | Complex SQL aggregation |
| Container stats (1h) | 3ms | Fast (8 data points) |

**Dashboard initial load** makes 4 parallel API calls: `/api/setup/status` (254ms), `/api/blackouts/active` (2ms), `/api/containers` (216ms), `/api/config` (2ms). The setup status check is the bottleneck — it runs every page load even after setup is complete. Could cache or skip when setup is done.

**Trends endpoint (363ms)** is the slowest — does window-based aggregation across all containers for 7d and 30d periods. Consider pre-computing or caching with a short TTL.

### Ideas for Future Development

- **Shared CSS file** — Extract common styles into `common.css` to eliminate ~600 lines of duplication across 4 HTML files
- **WebSocket upgrade** — Replace SSE with WebSocket for bidirectional communication (e.g., cancel evaluation, real-time log streaming)
- **Dark/light theme toggle** — Currently hardcoded dark theme only
- **Export digest as PDF or email** — Users may want to share daily digests with team members
- **Container grouping/tagging** — Dashboard could group containers by service type (media, infrastructure, databases) for better organization at scale
- **Notification history page** — No way to see past alerts; only the current state is visible
- **Health check response time charts** — Plot health check latency over time to catch degradation trends
- **Search/filter on events** — Events table needs text search and date range filtering
- **Bulk actions** — "Evaluate All" button, group restart from dashboard
- **Pre-compute trends** — Cache the `/api/trends` result with a 5-minute TTL to cut the 363ms query on every Insights page load
- **Skip setup check after first run** — `/api/setup/status` hits both Docker and Ollama APIs (254ms) on every page load. Cache the result or set a flag after first successful setup
- **Connection pool for SQLite** — Many functions open/close individual connections. A shared connection or pool would reduce overhead
- **Graceful shutdown** — Health checker and SSE connections have no clean shutdown path. Add asyncio cancellation support
- **Rate-limit sparkline requests** — Stagger or batch the 20 parallel stats requests on dashboard load

---

## Known Issues

1. **karakeep_chrome scores DEGRADED (60) despite being healthy** — OPEN BUG

   **Problem:** karakeep_chrome is a headless Chromium container. It logs 18 ERROR and 15 WARN lines at every startup — all normal artifacts of running without D-Bus, Bluetooth, audio, or a desktop. After ignore_pattern filtering, 37 lines survive with 0 INFO, 15 WARN, 18 ERROR. The LLM can't score this healthy because it sees 100% error/warning content.

   **What we tried (in order):**
   - Heavy ignore_patterns (22 patterns → auto-healthy): Worked but user rejected — "teach the AI, don't hide data"
   - Rolled back to 1 ignore_pattern (config_dir_policy_loader spam only)
   - context_prompt explaining every error type is normal + explicit scoring rules: LLM acknowledges context but still scores low
   - Few-shot example (normal Chromium startup scored 95/healthy): Helps but doesn't override severity counts
   - known_patterns with [ROUTINE:] tags on all 4 major error types: Tags visible but LLM still weighs raw counts
   - Switched qwen2.5:7b-instruct → llama3.1:8b: Improved 35→60 but still not 80+
   - Added preprocessing explanation to base prompt (v5_evaluate.txt): Marginal improvement
   - Added routine_counts to severity display (`18 (18 routine) ERROR`): LLM sees 0 real errors mathematically but still anchors on the raw count

   **Root cause:** Small LLMs (7-8B) have strong priors that errors=bad. When the severity header shows `18 ERROR`, no amount of in-context instruction fully overrides that anchor — especially when there are 0 INFO lines to provide a "healthy" signal.

   **Best fix candidates (for future sessions):**
   1. **Reclassify routine-tagged lines as INFO in preprocessor** — after tagging `[ROUTINE:]`, change the line's severity from ERROR→INFO. Counts become `33 INFO, 0 WARN, 0 ERROR`. Original severity preserved in line text. One-line change, stays true to "teach don't hide."
   2. **Extend auto-healthy fast path** — trigger when ALL error/warn lines are tagged ROUTINE (not just when all lines match ignore_patterns). Skip LLM entirely.
   3. **Larger model** — 13B+ models may handle context override better.

   **Relevant config (as of Session 4):**
   - 1 ignore_pattern: `config_dir_policy_loader`
   - Full context_prompt explaining all Chromium noise
   - 1 few-shot example (normal startup → 95/healthy)
   - 4 known_patterns with [ROUTINE:] tags (D-Bus, Bluetooth, sandbox, bluez)
2. **Docker image tag mismatch** — docker-compose.yml references `:latest` but active container runs `:dev`. Next GitHub Release will sync.
3. **Docker volume vs host DB** — RESOLVED. — container uses `dockmon-data:/app/data`. Host path `/home/o51r15/scripts/dockmon/data/dockllama.db` is separate. Don't confuse them.
4. **Evaluate Now doesn't update dashboard** — RESOLVED (Session 6). On-demand eval saves to events table but fetchContainers() still shows stale data. The /api/containers endpoint may return the previous eval cycle's cached result rather than the freshly inserted event. Needs investigation into whether the containers endpoint queries the latest event or uses an in-memory cache.
5. **Sparkline flicker causes layout shift** — RESOLVED (Session 6). CPU/RAM sparklines on dashboard cards disappear and reappear every few seconds, causing the entire grid to shift up and down. Likely caused by loadSparklines() clearing c._spark (triggering x-show hide) before the async fetch completes with new data. Fix: set new data atomically, or reserve sparkline height with CSS min-height so layout does not shift during loading.
6. **Ollama error response not handled — eval failures under GPU contention** — RESOLVED (Session 7, c012c32). When another process (video transcoder) uses the GPU, Ollama returns HTTP 200 with `{"error": "Connection refused"}`. `ai_engine.py` doesn't check for the `error` key — tries to parse `data["response"]` which doesn't exist, hits JSONDecodeError, retries (same failure), returns fallback score 50 / 0% confidence. Fix: check `if "error" in data:` before parsing, add retry with backoff.
7. **`_update_yaml_field()` regex backreference injection** — RESOLVED (Session 7). In `config.py`, `pattern.subn(r"\g<1>" + val_str, text)` interprets backslash sequences in val_str as regex backreferences. Fix: use lambda replacement.
8. **`save_containers_to_config()` destroys YAML comments** — RESOLVED (Session 7, c012c32). Switched to targeted text-block replacement. NOTE: the first version of this fix had a regression (see #14) where list-item detection duplicated the container list — fixed in 8a72f4b.
9. **`_config_path` Pydantic v2 private field** — RESOLVED (Session 7, c012c32). Now uses `PrivateAttr()`.
10. **DELETE body for container removal non-standard** — RESOLVED (Session 7, c012c32). Now uses query param `?purge=true`.
11. **Dead code in `addContainer()`** — RESOLVED (Session 7, c012c32). Reset moved into catch block.
12. **Dashboard multi-minute load / spinner** — RESOLVED (Session 7, f7e71db + 72e2c45). The eval loop's synchronous Docker SDK calls (get_logs, get_container_stats) blocked the asyncio event loop for the whole ~3-minute cycle, and `/api/containers` made 19 more Docker calls per request (1 list + 18 per-container image inspects) that queued behind the eval loop's stats saturation. Fix: (a) wrapped all blocking Docker/preprocessor calls in `asyncio.to_thread()` and yield between containers; (b) start the web server before startup_check; (c) cache the container snapshot (8s TTL) so the dashboard reads from memory, offload the single refresh list call to a thread, and read image names from already-loaded attrs instead of per-container image inspects. Dashboard now serves in 7-10ms mid-eval.
13. **Ollama model changed to phi4-mini via UI** — INFO, not a bug. During testing the default model was switched to `phi4-mini:latest` and poll interval to 345s. These persisted correctly to config.yaml (config persistence working as designed).
14. **Container list duplicated → dashboard cards never render** — RESOLVED (Session 7, 8a72f4b). The comment-preserving `save_containers_to_config` (fix for #8) detected the end of the `containers:` block by looking for the next line starting at column 0 with a colon — but YAML list items (`- name: x`) also start at column 0, so it stopped at the first entry and replaced only the `containers:` header, leaving the old entries in place. Every save doubled the list (18 → 36). Duplicate `name` values collided on Alpine's `x-for :key="c.name"`, which makes Alpine silently refuse to render the entire list — so the dashboard showed "loading" forever with no cards. Fix: exclude dash-prefixed lines from top-level-key detection. Live config de-duplicated back to 18 and container restarted.


---

## Model Benchmark Methodology (v2.0)

### Overview

The benchmark system tests Ollama models against 8 synthetic container health scenarios using DockLlama's own `evaluate()` pipeline — the same code path used in production evaluations. This ensures benchmark results accurately predict real-world model performance.

### Architecture

- **Hidden admin endpoints** at `/api/admin/benchmark*` (not linked from navigation)
- **Pause/resume eval cycle** via `POST /admin/eval/pause` and `/admin/eval/resume` to prevent Ollama model-swap contention during benchmarking
- **Single-scenario endpoint** `POST /admin/benchmark-scenario/{scenario_id}/{model}` runs one scenario at a time
- **Full benchmark endpoint** `POST /admin/benchmark/{model}` runs all 8 scenarios sequentially
- **Results persisted** in `benchmark_results` SQLite table with full JSON scenario details
- **Frontend** at `/admin-benchmark.html` (hidden, not in nav)

### Scenarios

Each scenario provides a synthetic `structured_summary` (the same format the preprocessor produces) to `EvaluationContext`, then scores the model's JSON response.

| ID | Name | Difficulty | Expected Status | Score Range | Key Challenge |
|----|------|-----------|----------------|-------------|---------------|
| S1 | Clean Healthy Container | Easy | healthy | 85-100 | Baseline: should score high on boring logs |
| S2 | OOM Crash Loop | Easy | critical | 0-15 | Obvious failure: OOM kills, high restart count |
| S3 | Recovered After Errors | Medium | healthy | 80-100 | Past errors but current state is clean |
| S4 | Routine Errors (Known Patterns) | Medium | healthy | 85-100 | DHT/tracker errors that are normal for BitTorrent |
| S5 | Degraded Performance | Medium | degraded | 40-70 | Slow responses, not failing but not healthy |
| S6 | Graceful Shutdown (FATAL is Normal) | Hard | healthy | 75-100 | PostgreSQL FATAL during planned restart |
| S7 | High CPU Normal Workload | Hard | healthy | 85-100 | 94% CPU from transcoding — working, not broken |
| S8 | External Dependency Failure | Hard | degraded | 30-65 | Stripe API down — container itself is fine |

### Scoring Rubric (per scenario, 100 points max)

- **Status correctness** (30 pts): exact match = 30, close match = 10-18, wrong = 0
- **Health score in range** (30 pts): in expected range = 30, within 10 points = 15, far off = 0
- **Action correctness** (20 pts): correct recommended_action = 20, wrong = 0
- **Restart recommendation** (10 pts): correct restart_would_help = 10, wrong = 0
- **Summary quality** (10 pts): non-trivial summary (>15 chars) = 10

### Running Benchmarks

1. Pause evaluations: `curl -X POST http://host:8556/api/admin/eval/pause`
2. Run scenarios one at a time per model (avoids Ollama model-swap contention with the eval cycle)
3. Results saved to `benchmark_results` DB table
4. Resume evaluations: `curl -X POST http://host:8556/api/admin/eval/resume`

The admin benchmark UI at `/admin-benchmark.html` provides a visual interface for running and viewing results.

### Key Findings

- **S4 (Routine Errors)** is the hardest differentiator — smaller models flag DHT/tracker noise as degraded
- **S8 (External Dependency)** trips most models into "unhealthy" instead of "degraded" — they conflate the container's health with the external API's health
- **S2 (OOM)** — all models correctly identify the crash but some miss the restart recommendation
- **Thinking models** (qwen3, deepseek-r1) work correctly since `format: "json"` was removed from Ollama payloads; the app now uses regex-based JSON extraction that strips `<think>` tags
- **phi4:latest** (14B) achieved near-perfect scores with 7/8 scenarios at 100 points
- **qwen2.5:7b-instruct** (7B) outperformed most 14B models — the best value pick
- **phi4-mini:latest** (3.8B) scored 91.5%, making it viable for resource-constrained setups


---

## Session 9 — Benchmarking All Models & Documentation

### What happened
- Ran the full 8-scenario benchmark suite against all 10 installed Ollama models
- First run had GPU contention — eval cycle was competing with benchmarks despite pause endpoint returning paused=true. Killed the stuck run and wrote bench_all2.sh that re-confirms pause before each model.
- Second run completed all 10 models in ~8 minutes with correct response times

### Benchmark v2.1 Results
| Model | Score | Grade | Avg Response |
|-------|-------|-------|-------------|
| phi4 (14B) | 780/800 | A+ | 4998ms |
| qwen2.5:7b-instruct | 770/800 | A+ | 2692ms |
| llama3.1:8b | 753/800 | A | 2768ms |
| qwen3:14b | 750/800 | A | 5206ms |
| deepseek-r1:14b | 750/800 | A | 4616ms |
| phi4-mini (3.8B) | 732/800 | A | 2194ms |
| gemma4 | 720/800 | A | 3643ms |
| gemma3:4b | 705/800 | B | 2830ms |
| llama3.2:3b | 675/800 | B | 1874ms |
| mistral:7b | 675/800 | B | 2811ms |

### Key findings
- qwen2.5:7b-instruct outperforms all 14B models except phi4 at half the VRAM and twice the speed
- phi4-mini is the standout small model — 91.5% accuracy at 2.5GB VRAM
- Thinking models (qwen3, deepseek-r1) are capable but slower due to chain-of-thought overhead
- Results consistent with v2.0 benchmarks, confirming scoring stability

### Documentation updates
- Updated README.md Model Recommendations section with v2.1 data and methodology (commit f84b1f4)
- Created HANDOFF.md with full project documentation for session continuity
- Created blog post 27.txt covering the entire project history and architecture

### Scripts created
- bench_all2.sh — sequential 10-model benchmark with per-model pause confirmation
- patch_readme2.py — automated README update with v2.1 results

### Git commits
- f84b1f4 — docs: update README with v2.1 benchmark results and methodology
