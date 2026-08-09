<div align="center">

<img src="assets/banner.svg" alt="dockLlama banner" width="600">

**AI-powered Docker container health monitoring with local LLMs.**

Watches your containers, analyzes logs with Ollama, and tells you what's wrong — no cloud, no API keys, no log shipping.

[![GitHub last commit](https://img.shields.io/github/last-commit/o51r15/DockLlama)](https://github.com/o51r15/DockLlama)
[![Docker Image](https://img.shields.io/badge/ghcr.io-dockllama%3Adev-blue)](https://ghcr.io/o51r15/dockllama)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

</div>

---

> **Alpha** — functional and monitoring 18+ containers in production, but under active development. Expect rough edges.

## What It Does

dockLlama runs on a loop: it pulls logs from every container you configure, preprocesses them to strip noise, sends a structured summary to a local LLM (via Ollama), and records the health verdict. If something looks wrong, it can alert you or (optionally) restart the container automatically.

Unlike log aggregators (Loki, ELK) or uptime monitors (Uptime Kuma), dockLlama doesn't just detect *that* something failed — it uses an LLM to explain *what* failed and *why*, with root cause classification and recommended actions.

## Features

- **AI Log Evaluation** — each container's logs are analyzed by a local LLM with structured JSON output (status, health score, confidence, root cause, recommended action)
- **Structured Preprocessor** — Python-based log analyzer handles severity counting, error deduplication, recovery detection, and timestamp parsing *before* the LLM sees anything. Even 3B models get accurate results.
- **Per-Container Tuning** — custom system prompts, few-shot examples, and known-pattern tags teach the AI what's normal for each container
- **Knowledge Base** — teach DockLlama about expected patterns to reduce false positives; classified patterns are injected into future AI evaluations
- **Pattern Detection** — automatic pattern tracking with user-driven classification workflow
- **Trend Analysis** — longitudinal health scoring with fleet-wide trend reporting
- **Resource Monitoring** — CPU and memory stats with sparklines on the dashboard and historical charts per container
- **Smart Restart** — optional automatic restarts with exponential backoff, dependency-aware group ordering, and boot-loop protection
- **Container Management** — add/remove monitored containers from the UI with config archival and optional data purge
- **Multi-Node Fleet** — monitor containers across multiple Docker hosts (TCP, SSH, TLS)
- **Model Selection** — discover, benchmark, and switch Ollama models from the settings UI with hardware-aware interval recommendations
- **Daily Digest** — LLM-generated fleet summary with 24h trends and recommendations
- **Notifications** — Discord, Gotify, Telegram, email, and 80+ services via Apprise
- **Web Dashboard** — real-time status grid with SSE live updates, sparklines, and detail drawers
- **Log Explorer** — browse raw/filtered logs, trigger on-demand evaluations, view event history
- **Prompt Editor** — customize AI instructions per container with live testing against real logs
- **Prometheus Metrics** — `/api/metrics` endpoint for Grafana/Prometheus integration
- **Config Hot-Reload** — edit config.yaml without restarting the container
- **Health Checks** — HTTP/TCP endpoint monitoring alongside container evals
- **Blackout Windows** — suppress evals during maintenance periods
- **Dry-Run Mode** — evaluate and recommend without taking action (default)
- **SQLite Storage** — WAL-mode database for events, stats, baselines, cooldowns, and model test results

## Quick Start

### Docker (recommended)

```bash
# 1. Create your config
mkdir -p ~/dockllama
curl -o ~/dockllama/config.yaml \
  https://raw.githubusercontent.com/o51r15/DockLlama/main/config.example.yaml

# 2. Edit config.yaml — set your Ollama URL and containers to monitor
nano ~/dockllama/config.yaml

# 3. Find your host's docker group ID (needed for non-root container access)
DOCKER_GID=$(getent group docker | cut -d: -f3)

# 4. Run
docker run -d \
  --name dockLlama \
  --restart unless-stopped \
  --group-add $DOCKER_GID \
  -p 8556:8556 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v ~/dockllama/config.yaml:/app/config/config.yaml \
  -v dockllama-data:/app/data \
  -e TZ=America/New_York \
  ghcr.io/o51r15/dockllama:dev
```

> **Note:** `--group-add $DOCKER_GID` grants the non-root container user access to the Docker socket. The container runs as an unprivileged `dockllama` user for security.

Dashboard: **http://localhost:8556**

### From Source

```bash
git clone https://github.com/o51r15/DockLlama.git
cd DockLlama
cp config.example.yaml config.yaml
# Edit config.yaml

pip install -r requirements.txt
python -m dockllama config.yaml
```

Requires Python 3.12+ and an accessible Ollama instance with at least one model pulled.

## Configuration

See [`config.example.yaml`](config.example.yaml) for all options. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama.base_url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama.default_model` | `llama3.1:8b` | Model for log evaluation |
| `ollama.digest_model` | `llama3:8b` | Model for daily digest summaries |
| `monitoring.poll_interval_seconds` | `60` | Seconds between monitor cycles |
| `monitoring.dry_run` | `true` | Evaluate only, no restarts |
| `monitoring.eval_retention_days` | `30` | Days to keep eval history |
| `cooldowns.initial_minutes` | `5` | Cooldown after first restart |
| `cooldowns.max_restarts_per_hour` | `3` | Boot-loop breaker threshold |
| `digest.schedule_cron` | `0 7 * * *` | Daily digest time (UTC) |
| `digest.digest_window_hours` | `24` | Hours of data included in digest |

### Per-Container Config

```yaml
containers:
  - name: postgres
    enabled: true
    node: local                       # which Docker node (default: local)
    context_prompt: "This is a PostgreSQL database. Planned restarts with FATAL shutdown messages are normal maintenance."
    ignore_patterns:
      - "checkpoint starting"
      - "redo done at"
    model_override: "qwen2.5:14b"    # use a bigger model for critical containers
    compose_group: "database-stack"   # restart group ordering
```

### Multi-Node Fleet

Monitor containers across multiple Docker hosts:

```yaml
docker_nodes:
  - name: local
    url: unix:///var/run/docker.sock
  - name: remote-server
    url: tcp://192.168.1.100:2376
    tls: true
    tls_ca: /certs/ca.pem
    tls_cert: /certs/cert.pem
    tls_key: /certs/key.pem
  - name: pi-cluster
    url: ssh://user@192.168.1.50

containers:
  - name: remote-app
    node: remote-server
    enabled: true
```

Config changes are picked up automatically — no container restart needed.

## How It Works

```
Container Logs
  → Log Analyzer (severity count, error dedup, recovery detection)
  → Structured Summary (~20 lines instead of ~200 raw)
  → Knowledge Base Injection (user-classified patterns added to context)
  → Ollama LLM (structured JSON: status, score, confidence, root cause, action)
  → Pattern Tracker (auto-detect recurring issues, surface for classification)
  → Action Engine (cooldown check → restart or dry-run log)
  → Alerts (Apprise) + SSE (dashboard) + SQLite (events)
```

The preprocessor does all mechanical analysis in Python — the LLM only interprets patterns. This means model size matters far less than with raw log parsing, and even 3B models get correct results on clear-cut cases.

## Model Recommendations

All 10 models were benchmarked against 8 synthetic container health scenarios using DockLlama's own `evaluate()` pipeline — the same code path used in production. Each scenario provides a structured log summary and scores the model's JSON response across 5 dimensions: status correctness (30 pts), health score accuracy (30 pts), action correctness (20 pts), restart recommendation (10 pts), and summary quality (10 pts). Total: 800 points across all 8 scenarios.

Scenarios range from easy (clean healthy container, obvious OOM crash) to hard (PostgreSQL FATAL during graceful shutdown, high CPU from normal transcoding, external API failure vs container failure). The hardest differentiators are S4 (routine BitTorrent tracker errors that smaller models flag as degraded) and S8 (external dependency failure where models conflate the container's health with the API's health).

### Benchmark Results (v2.1)

| Rank | Model | Size | Score | Grade | Avg Response | Tier |
|------|-------|------|-------|-------|-------------|------|
| 1 | `phi4:latest` | 9.1 GB | 780/800 (97.5%) | A+ | ~5.0s | High |
| 2 | `qwen2.5:7b-instruct` | 4.7 GB | 770/800 (96.3%) | A+ | ~2.7s | Standard |
| 3 | `llama3.1:8b` | 4.9 GB | 753/800 (94.1%) | A | ~2.8s | Standard |
| 4 | `qwen3:14b` | 9.3 GB | 750/800 (93.8%) | A | ~5.2s | High |
| 4 | `deepseek-r1:14b` | 9.0 GB | 750/800 (93.8%) | A | ~4.6s | High |
| 6 | `phi4-mini:latest` | 2.5 GB | 732/800 (91.5%) | A | ~2.2s | Low |
| 7 | `gemma4:latest` | 9.6 GB | 720/800 (90.0%) | A | ~3.6s | High |
| 8 | `gemma3:4b` | 3.3 GB | 705/800 (88.1%) | B | ~2.8s | Standard |
| 9 | `llama3.2:3b` | 2.0 GB | 675/800 (84.4%) | B | ~1.9s | Low |
| 9 | `mistral:7b` | 4.4 GB | 675/800 (84.4%) | B | ~2.8s | Standard |

### Hardware Tier Guide

**High Tier (14B models, 9-10 GB VRAM):** Best accuracy. `phi4:latest` leads at 97.5% — near-perfect on 7/8 scenarios. The thinking models (`qwen3:14b`, `deepseek-r1:14b`) both score A but take longer due to chain-of-thought reasoning. Ideal when you have a dedicated GPU and fewer containers.

**Standard Tier (7-8B models, 4-5 GB VRAM):** Best value. `qwen2.5:7b-instruct` (A+, 96.3%) outperforms every 14B model except phi4 at half the VRAM and twice the speed. `llama3.1:8b` (A, default) is the reliable all-rounder.

**Low Tier (1-4B models, 2-3 GB VRAM):** For constrained hardware or large fleets. `phi4-mini:latest` (A, 91.5%) punches well above its weight at just 2.5 GB. Even `llama3.2:3b` (B) handles clear-cut healthy/critical cases correctly.

Models can be benchmarked and switched from **Settings > Models** in the UI, with hardware-aware interval calculations. Benchmark results persist in the database even if models are later removed.

## API

All endpoints are under `/api/`. Key routes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Docker, Ollama, DB connectivity |
| `/api/config` | GET | Running configuration |
| `/api/stream` | GET | SSE stream for live dashboard |
| `/api/containers` | GET | Container list with latest evaluations |
| `/api/containers/{name}/evaluate` | POST | On-demand AI evaluation |
| `/api/containers/{name}/logs` | GET | Filtered logs with stats |
| `/api/containers/{name}/stats` | GET | CPU/memory history (1h/24h/7d/30d) |
| `/api/containers/{name}/evals` | GET | Evaluation history |
| `/api/containers/{name}/prompt` | GET/PUT/DELETE | Per-container prompt config |
| `/api/containers/{name}/test-prompt` | POST | Test prompt against live logs |
| `/api/containers/{name}/knowledge` | GET/POST/PUT/DELETE | Knowledge base entries |
| `/api/containers/{name}/trend-analysis` | GET/POST | Trend analysis |
| `/api/containers/{name}/healthcheck` | GET/PUT | HTTP health check config |
| `/api/docker/containers` | GET | All Docker containers (for add/remove UI) |
| `/api/containers/add` | POST | Add container to monitoring |
| `/api/containers/{name}` | DELETE | Remove container (with optional `?purge=true`) |
| `/api/patterns/pending` | GET | Pending patterns awaiting classification |
| `/api/patterns/pending/counts` | GET | Pattern counts per container |
| `/api/patterns/pending/{id}/classify` | POST | Classify a pattern |
| `/api/events` | GET | Paginated event history |
| `/api/digest` | POST | Trigger daily digest |
| `/api/models` | GET | Available Ollama models |
| `/api/models/test` | POST | Benchmark a model |
| `/api/models/interval-calc` | GET | Hardware-aware interval calculator |
| `/api/stats/fleet` | GET | Fleet-wide resource overview |
| `/api/trend-analysis/fleet` | GET | Fleet-wide trend analysis |
| `/api/fleet/nodes` | GET | Docker node connection status |
| `/api/fleet/containers` | GET | All containers across all nodes |
| `/api/metrics` | GET | Prometheus-compatible metrics |
| `/api/help-links` | GET | Documentation links |

Full reference: [docs/api-reference.md](docs/api-reference.md)

## Documentation

- [Setup Guide](docs/setup-guide.md)
- [Configuration](docs/configuration.md)
- [Knowledge Base](docs/knowledge-base.md)
- [Prompt Customization](docs/prompt-customization.md)
- [API Reference](docs/api-reference.md)
- [Troubleshooting](docs/troubleshooting.md)

## Requirements

- Docker host with `/var/run/docker.sock` accessible
- [Ollama](https://ollama.com) instance with at least one model pulled (can be on a different machine)
- Python 3.12+ (if running outside Docker)

## Tech Stack

Python 3.12+, FastAPI, Alpine.js, Chart.js, SQLite (WAL mode), Ollama, Apprise, httpx

## License

[MIT](LICENSE)
