# Setup in detail

## Ollama on another computer

Ollama only answers on its own computer unless you change one setting. If DockLlama runs on a
different machine (or in Docker on the same one), set `OLLAMA_HOST` on the Ollama machine:

- **Windows:** quit Ollama from the system tray, open *Edit environment variables for your account*,
  add `OLLAMA_HOST` = `0.0.0.0:11434`, start Ollama again, and allow private networks if the firewall asks.
- **Mac:** `launchctl setenv OLLAMA_HOST "0.0.0.0:11434"`, then quit and reopen Ollama.
- **Linux:** `sudo systemctl edit ollama.service`, add
  ```
  [Service]
  Environment="OLLAMA_HOST=0.0.0.0:11434"
  ```
  then `sudo systemctl daemon-reload && sudo systemctl restart ollama`.
- **Ollama in Docker:** publish the port (`ports: ["11434:11434"]`) and use that machine's IP.

Then type just the IP or name in DockLlama (it adds `http://` and `:11434`). Ollama 0.9.0 or newer is needed.

DockLlama looks for Ollama by itself at `host.docker.internal`, `ollama` (a container next to it) and
the Docker gateway, so on the same machine you often don't have to type anything.

## No Ollama yet

Install it from [ollama.com/download](https://ollama.com/download), on a computer with a graphics card
if you have one, or add it next to DockLlama in the same compose file:

```yaml
  ollama:
    image: ollama/ollama
    container_name: ollama
    restart: unless-stopped
    volumes:
      - ollama:/root/.ollama
```

and add `ollama:` under `volumes:` at the bottom. DockLlama finds it as `ollama:11434`. You can also
**continue without AI** and add it later in Settings → AI.

## Keeping the data in a folder

The compose file keeps DockLlama's database in a Docker volume. To use a folder instead, change
`dockllama-data:/app/data` to e.g. `./data:/app/data`. DockLlama makes that folder owned by its own
user (1000). To choose the owner yourself, set `PUID` and `PGID` (for example 99 and 100 on Unraid):

```yaml
    environment:
      PUID: "99"
      PGID: "100"
```

## Docker socket permissions

You don't need `--group-add` or a `user:` line. DockLlama starts as root just long enough to match
the Docker socket's group and fix the data folder's owner, then drops to its own user. If you add a
`user:` line yourself, DockLlama can't do that and will say it isn't allowed to use Docker.

## Time zone

The setup guide takes your browser's time zone, so quiet hours and the daily summary use your local
time without setting `TZ`.
