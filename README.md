<p align="center"><img src="assets/banner.svg" alt="DockLlama" width="600"></p>

# DockLlama v2 (in development)

DockLlama watches your Docker containers and uses a local AI (Ollama) to explain, in plain words,
which ones need attention. **v2 is a rebuild** that sets everything up from the web page: no config
files, no group IDs to look up.

> v2 is not finished. The v1 code is kept under the [`v1-final`](../../tree/v1-final) tag.
> Progress follows the build phases in the v2 plan.

## What works so far (phases 1–3)

- One-command install with Docker Compose
- First-run password setup and sign-in; change password in Settings
- Automatic Docker socket permissions (no `--group-add`)
- Settings → Containers: every container on this machine with its state in plain words, and a
  switch to choose which ones DockLlama watches
- Settings → AI: finds Ollama automatically (or type its address), suggests a model that fits your
  hardware, lets you pick any installed model, one-click downloads of suggested models, and an
  Advanced panel per model (thinking, context size, and more — only what that model supports)

### Ollama on another computer

Ollama only listens on its own computer by default. On the Ollama machine set `OLLAMA_HOST=0.0.0.0`
and restart Ollama (Settings → AI shows the exact steps for Windows, Mac, Linux and Docker).
Ollama 0.9.0 or newer is required.

## Install

```bash
curl -O https://raw.githubusercontent.com/o51r15/DockLlama/v2/docker-compose.yml
docker compose up -d
```

Then open `http://<this machine's IP>:8556`. (On Linux, `hostname -I` shows the IP.)

Supported: Docker Engine on Linux, Docker Desktop (Windows/Mac), Unraid, Synology. Not supported
yet: Podman and rootless Docker.

**Keep DockLlama off the internet.** Access to the Docker socket means control of the machine.

### Using a folder instead of a Docker volume

If you change `dockllama-data:/app/data` to a folder like `./data:/app/data`, DockLlama makes that
folder owned by its own user (1000). To pick the owner yourself, set `PUID` and `PGID`, e.g. `99`
and `100` on Unraid.

## Forgot your password?

On the machine running DockLlama:

```bash
docker exec -it dockllama dockllama reset-password
```

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```
