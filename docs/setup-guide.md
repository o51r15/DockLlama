# DockLlama Setup Guide

## Prerequisites

- **Docker** running on the host you want to monitor
- **Ollama** accessible at a network URL (e.g., `http://192.168.1.125:11434`)
- At least one Ollama model pulled (recommended: `llama3.1:8b` or `phi4:latest`)

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

## First-Run Setup Wizard

On first launch with no configuration, DockLlama shows a setup wizard:

1. **Ollama Connection** — Enter your Ollama URL, test connectivity
2. **Model Selection** — Pick a model, run validation tests
3. **Container Selection** — Choose which Docker containers to monitor
4. **Interval Recommendation** — Auto-calculated safe poll interval

## Docker Log Retention

**Important:** DockLlama uses Docker's raw logs for context during evaluations. Configure Docker log rotation appropriately:

```json
// /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
```

If Docker rotates logs before `eval_retention_days` expires, the learning system can't pull raw context for older evaluations.

## Ollama Model Recommendations

| Model | Size | Speed | Quality | Best For |
|-------|------|-------|---------|----------|
| `llama3.1:8b` | 4.7GB | Fast | Good | Default, most containers |
| `phi4:latest` | 9.1GB | Medium | Better | Complex log analysis |
| `gemma4:latest` | — | Medium | Good | Digest generation |

Use the Model Validation page (Settings → Models) to benchmark models on your hardware.
