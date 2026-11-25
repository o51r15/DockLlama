# How checks work

Every 10 minutes (Settings → Checking and fixing), for each watched container:

1. **Docker's facts:** state, exit code, restarts, out-of-memory kills, Docker's own health check,
   CPU and memory.
2. **Rules that need no AI:**
   - exited with code 0, paused, or stopped by you (`docker stop`, `compose down`) → **Stopped**, no alert;
   - exited with an error, or killed (`docker kill`) → **Down or failing**;
   - out of memory, or restarting again and again → **Down or failing**;
   - failing its health check → **Having problems**;
   - crashed once and restarted by Docker → **Needs a look**.

   `docker stop` and `docker kill` both end with code 137; DockLlama tells them apart from Docker's
   event log, which it also listens to as events happen.
3. **New log lines** since the last check (up to 300), each cut at 2,000 characters.
4. **Passwords, tokens and keys are masked** in those lines before anything is stored, shown, sent to
   the AI or put in a message.
5. **Summarised:** counts by severity, repeated errors grouped, whether it recovered, the last lines.
   Lines matching a container's known patterns count as routine, and your notes ("what's normal")
   go along with it.
6. **The AI reads the summary** and answers in a fixed format: status, a short explanation, what to
   do, and whether a restart would help. If nothing changed since the last check (no new lines, same
   Docker facts), it isn't asked again, except at least every 6 hours.
7. **The worse of the two wins**: the AI's verdict or the rules'. If the AI can't answer, the status
   is **Couldn't check** with the reason, never "fine".

Without AI, steps 1–2 still run every round, so crashes, restart loops, out-of-memory kills and
health checks are still caught.

A container that hangs silently (stops logging, has no Docker health check) still looks fine from its logs and Docker's state. The **web page check** below covers that.

## Web page check

On a container's page, under **Web page check**, give the local address of its web page. DockLlama suggests one from the container's published ports, so usually you just click it, then **Try it** and **Save**.

At each check DockLlama opens that address. Any answer counts as answering, including a login page or "not found", because it shows the app is alive. A connection that's refused, no answer within 10 seconds, or a server error (HTTP 500 and up) counts as not answering. After **3 checks in a row** without an answer, the container is marked **Having problems**: "Its web page stopped answering, but the container is still running, so it may be frozen." With automatic restarts allowed for that container, this can trigger a restart, like a failing Docker health check. The usual limits, cooldown and quiet hours apply.

**Only local addresses work**: this machine (`host.docker.internal`), an IP on your network (`192.168.x.x`, `10.x.x.x`), a Tailscale `100.x` address, a container name, or a `.lan` / `.local` name. A public address like `https://jellyfin.example.com` is refused. It goes through Cloudflare or a reverse proxy, which can answer with its own error page while the app behind it is frozen, so a check would say "fine" when it isn't. That's why uptime monitors that check public URLs miss this.

Optional: **the page must contain a word.** For the rare app that shows an error page with a normal answer code. When a word is set, DockLlama follows redirects that stay on the same local address to find it.

For a container on another machine (DockLlama Remote), use that machine's IP and the published port; DockLlama checks it over your network.
