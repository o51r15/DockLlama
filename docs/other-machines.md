# Watching other machines

Most people run DockLlama on the one machine it watches. To watch several Docker machines from one
dashboard, run **DockLlama Remote** on each of the others. Docker only shows a machine's containers to
programs on that machine, so this small helper answers DockLlama for it. It's the same image with a
different command: no AI, no web page, no database.

1. Settings → Machines → **Add another machine**. Choose whether DockLlama may restart containers there.
2. DockLlama shows a compose file with a new pairing token in it. On the other machine, save it as
   `docker-compose.yml` in a new folder and run `docker compose up -d`:
   ```yaml
   services:
     dockllama-remote:
       image: ghcr.io/o51r15/dockllama:latest
       container_name: dockllama-remote
       command: ["dockllama", "remote"]
       restart: unless-stopped
       ports:
         - "8557:8557"
       volumes:
         - /var/run/docker.sock:/var/run/docker.sock
       environment:
         DOCKLLAMA_REMOTE_TOKEN: "…made for you…"
         DOCKLLAMA_REMOTE_RESTARTS: "no"
   ```
3. Back in DockLlama, give it a name and type its IP address, then **Test and add**.
4. Pick its containers in Settings → Containers.

What Remote shares, only with the token: the container list, their state, logs, CPU/memory and
Docker's events, and restarts if `DOCKLLAMA_REMOTE_RESTARTS` is `yes`. It never shares containers'
environment variables, mounts or other settings, which often hold passwords. Traffic is plain HTTP on
your LAN, like Ollama's. Keep port 8557 off the internet.

If a machine can't be reached, only its containers show **Couldn't check**, and you get one message
after 3 rounds.
