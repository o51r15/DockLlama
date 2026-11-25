<p align="center"><img src="assets/banner.svg" alt="DockLlama" width="600"></p>

# DockLlama

DockLlama watches your Docker containers and tells you, in plain words, which ones need attention
and what to do about it. It reads Docker's own information (crashes, restart loops, out-of-memory
kills, health checks) and, if you have [Ollama](https://ollama.com), uses a local AI to read the
containers' logs. Your logs never leave your network.

Everything is set up from the web page: no config files, no group IDs, nothing to edit.

> **v2 is in testing.** Until the 2.0 release the image tag is `v2-dev`. The old v1 code is kept under
> the [`v1-final`](../../tree/v1-final) tag. Coming from v1? See [Upgrading from v1](docs/upgrading-from-v1.md).

## Install

You need Docker. Save this as `docker-compose.yml` (or download it with the first line below) and start it:

```bash
curl -O https://raw.githubusercontent.com/o51r15/DockLlama/v2/docker-compose.yml
docker compose up -d
```

```yaml
services:
  dockllama:
    image: ghcr.io/o51r15/dockllama:v2-dev
    container_name: dockllama
    restart: unless-stopped
    ports:
      - "8556:8556"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - dockllama-data:/app/data
      - ./backups:/app/backups
    extra_hosts:
      - "host.docker.internal:host-gateway"
volumes:
  dockllama-data:
```

Then open **`http://<this machine's IP>:8556`** in a browser. (On Linux, `hostname -I` shows the IP.)
A short setup guide takes it from there.

Runs on Docker Engine on Linux, Docker Desktop (Windows/Mac), Unraid and Synology, on Intel/AMD and
ARM (Raspberry Pi 4/5). Not supported yet: Podman and rootless Docker.

**Keep DockLlama off the internet.** Access to the Docker socket means control of the machine. The
socket is mounted without `:ro` on purpose: `:ro` doesn't stop changes through a socket, so it only
gives a false sense of safety.

## The setup guide

1. **Create a password** (or skip it on a private network, with a warning).
2. **Connect your AI.** DockLlama looks for Ollama by itself. If nothing answers, it shows the one
   setting to change on Windows, Mac, Linux or Docker. No Ollama? Add it next to DockLlama with the
   snippet it gives you, or **continue without AI**: DockLlama still watches every container's state,
   crashes, restart loops, out-of-memory kills and health checks.
3. **Pick a model.** Tell it what the Ollama machine has (graphics card size), and DockLlama suggests a
   model. **Quick start** uses it right away and tests the others in the background; **Test all now**
   lets you watch every model being tested first. Suggested downloads are one click.
4. **Pick containers.** Running ones are ticked; DockLlama itself and the Ollama container aren't.
5. **Notifications** (optional): ntfy (recommended; free phone app, with a QR code), Discord,
   Telegram, Gotify, Pushover, email, or any Apprise URL. Send test, done.

Everything can be changed later in **Settings**, and the guide can be run again from Settings → Account.

## What you get

- **A dashboard** that puts problems first, each with a plain status, a one-line explanation,
  **what to do**, and the evidence (the log lines and Docker facts behind it). Everything that's fine
  is a small tile. Works on a phone.

  | Shown | Means |
  |---|---|
  | Working fine | Nothing to do |
  | Needs a look | A minor issue; it's still working |
  | Having problems | Something is wrong |
  | Down or failing | It's down, crashing or about to fail |
  | Stopped | It exited normally or someone stopped it (no alert) |
  | Couldn't check | DockLlama couldn't check it, with the reason |

- **A page per container**: history, CPU and memory for the last day, recent logs (with passwords and
  tokens masked), Check now, Restart, and **Tell DockLlama what's normal** ("This is a VPN; brief
  reconnects are normal").
- **Messages when something breaks and when it recovers**, not every check. Optional "still broken"
  reminders, a daily summary, and quiet hours.
- **Optional automatic restarts**, off by default, only for containers you allow and only when a
  restart would help. See [Restarts](docs/restarts.md).
- **Model testing on your own computer.** Every model can be tested with the same 8 practice
  questions; DockLlama suggests the best one for your hardware, and you choose.
- **Other Docker machines** on the same dashboard, with a small helper (DockLlama Remote). See
  [Watching other machines](docs/other-machines.md).

## Learn more

- [Setup in detail](docs/setup.md): Ollama on another computer, data folders, PUID/PGID
- [AI and models](docs/ai-and-models.md): suggestions, testing, the Advanced panel
- [How checks work](docs/how-checks-work.md)
- [Notifications](docs/notifications.md)
- [Restarts and quiet hours](docs/restarts.md)
- [Watching other machines](docs/other-machines.md)
- [Backup and restore](docs/backup.md)
- [Upgrading from v1](docs/upgrading-from-v1.md)
- [Troubleshooting](docs/troubleshooting.md)

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

Pushes to the `v2` branch build multi-arch images (`linux/amd64`, `linux/arm64`) as
`ghcr.io/o51r15/dockllama:v2-dev`.
