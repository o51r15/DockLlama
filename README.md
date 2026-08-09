# DockLlama

AI-powered Docker container health monitoring using local LLMs via [Ollama](https://ollama.ai).

DockLlama watches your Docker containers, analyzes their logs, resource usage, and health checks with a local AI model, then sends intelligent alerts when things go wrong.

## Features

- **AI Health Evaluations** — Ollama-powered log analysis with configurable models and prompts
- **Real-Time Dashboard** — Alpine.js + Chart.js web UI with live stats, eval history, and fleet overview
- **Smart Alerting** — Apprise-based notifications (Discord, Slack, email, etc.) with digest scheduling
- **Knowledge Base** — Teach DockLlama about expected patterns to reduce false positives
- **Pattern Detection** — Automatic pattern tracking with user-driven classification
- **Trend Analysis** — Longitudinal health scoring with fleet-wide trend reporting
- **Multi-Node Fleet** — Monitor containers across multiple Docker hosts (TCP, SSH, TLS)
- **Health Checks** — HTTP/TCP endpoint monitoring alongside container evals
- **Prometheus Metrics** — `/api/metrics` endpoint for Grafana integration
- **Config Hot-Reload** — Edit config.yaml without restarting the container
- **Blackout Windows** — Suppress evals during maintenance periods
- **Custom Prompts** — Per-container and base prompt customization

## Quick Start

```bash
docker run -d \
  --name dockLlama \
  --restart unless-stopped \
  -p 8556:8556 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v ./config.yaml:/app/config/config.yaml \
  -v dockmon-data:/app/data \
  -e TZ=America/New_York \
  ghcr.io/o51r15/dockllama:dev
```

Open `http://your-host:8556` in a browser.

## Configuration

Create a `config.yaml`:

```yaml
ollama:
  base_url: http://host.docker.internal:11434
  model: llama3.2

monitoring:
  poll_interval_seconds: 300
  db_path: /app/data/dockllama.db
  eval_retention_days: 30

alerting:
  apprise_urls:
    - discord://webhook_id/webhook_token

containers:
  - name: my-container
    enabled: true
```

See [docs/configuration.md](docs/configuration.md) for all options.

## Multi-Node Setup

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

containers:
  - name: remote-app
    node: remote-server
    enabled: true
```

## API

All endpoints are under `/api/`. Key routes:

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Service health |
| `GET /api/containers` | Monitored containers + status |
| `GET /api/containers/{name}/stats` | Resource stats history |
| `GET /api/containers/{name}/evals` | Evaluation history |
| `GET /api/containers/{name}/knowledge` | Knowledge base entries |
| `GET /api/patterns/pending` | Patterns awaiting classification |
| `GET /api/metrics` | Prometheus metrics |
| `GET /api/fleet/nodes` | Docker node status |
| `GET /api/help-links` | Documentation links |

Full reference: [docs/api-reference.md](docs/api-reference.md)

## Documentation

- [Setup Guide](docs/setup-guide.md)
- [Configuration](docs/configuration.md)
- [Knowledge Base](docs/knowledge-base.md)
- [Prompt Customization](docs/prompt-customization.md)
- [API Reference](docs/api-reference.md)
- [Troubleshooting](docs/troubleshooting.md)

## Tech Stack

- **Backend:** Python 3.12, FastAPI, SQLite (WAL mode)
- **Frontend:** Alpine.js, Chart.js, vanilla CSS
- **AI:** Ollama (any model — llama3.2, mistral, gemma2, etc.)
- **Alerts:** Apprise (70+ notification services)
- **Container:** Python 3.12-slim, non-root user

## License

MIT
