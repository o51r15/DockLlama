# Troubleshooting

## Common Issues

### Ollama Connection Failed
- Verify Ollama is running: `curl http://<ollama-host>:11434/api/tags`
- Check firewall rules between DockLlama and Ollama hosts
- Ensure the model is pulled: `ollama pull llama3.1:8b`

### Evaluations Timeout
- Check Ollama GPU memory — larger models need more VRAM
- Try a smaller model (llama3.1:8b vs phi4)
- Increase `ollama.timeout` in config.yaml

### False Positives (Healthy Container Flagged)
1. Add a `context_prompt` explaining normal behavior
2. Add `known_patterns` to tag routine log entries
3. Use few-shot `examples` with correct scores
4. Classify recurring patterns via the Knowledge Base

### High CPU During Eval Cycles
- Increase `poll_interval` — use the interval calculator in Settings
- The safe interval formula: `(avg_eval_time × container_count) + 300s`

### Docker Log Retention
If the learning system reports "unable to fetch logs for timestamp":
- Docker has rotated the logs before eval retention expired
- Increase Docker's `max-size` and `max-file` in `/etc/docker/daemon.json`
- Or reduce `eval_retention_days` in DockLlama config

### Database Growing Large
- Reduce `retention_days` (default 90)
- Reduce `stats_retention_days` (default 7)
- Reduce `eval_retention_days` (default 10)
- DockLlama auto-prunes daily; manual: restart the container

### Container Not Being Monitored
- Check Settings → Containers — is it enabled?
- Verify the container name matches exactly (case-sensitive)
- Check `docker logs dockLlama` for connection errors
