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

Known blind spot: a container that hangs silently (stops logging, no health check) still looks fine.
Adding a Docker health check to it fixes that.
