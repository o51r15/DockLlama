# Configuration Reference

## config.yaml Structure

```yaml
monitoring:
  poll_interval: 445          # Seconds between eval cycles
  db_path: /app/data/dockllama.db
  retention_days: 90          # Event history retention
  stats_retention_days: 7     # Resource stats retention
  eval_retention_days: 10     # Eval history retention
  dry_run: true               # true = no actual restarts

ollama:
  url: http://192.168.1.125:11434
  model: phi4:latest          # Default eval model
  timeout: 120

digest:
  enabled: true
  schedule_cron: "0 7 * * *"  # Daily at 7 AM
  digest_window_hours: 24     # Hours of data to include
  model: gemma4:latest        # Model for digest generation

containers:
  - name: "gluetun"
    enabled: true
    context_prompt: |          # Optional AI context
      This is a VPN tunnel container...
    examples: []               # Few-shot learning examples
    known_patterns: []         # Metadata tagging patterns
    ignore_patterns:           # Log lines to skip
      - "INFO.*routine"

dependency_groups:
  vpn_stack:
    - gluetun
    - qbittorrent
    - prowlarr
```

## Per-Container Overrides

Each container can override the global model via Settings → Containers or the Prompt Editor.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `UTC` | Timezone for logs and scheduling |
| `CONFIG_PATH` | `/app/config/config.yaml` | Config file location |
