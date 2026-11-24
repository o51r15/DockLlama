"""Checks that need no AI (plan section 6, step 2). They run every round, with or without AI.

Statuses (internal → shown): healthy → Working fine, degraded → Needs a look,
unhealthy → Having problems, critical → Down or failing, stopped → Stopped, unknown → Couldn't check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITY = {"healthy": 0, "stopped": 0, "unknown": 1, "degraded": 2, "unhealthy": 3, "critical": 4}

SIGNALS = {130: "SIGINT", 137: "SIGKILL", 139: "SIGSEGV (crashed)", 143: "SIGTERM"}


@dataclass
class Facts:
    """What Docker says about a container right now (no logs)."""
    name: str
    state: str  # running | exited | restarting | paused | created | dead | removing
    exit_code: int | None = None
    oom_killed: bool = False
    restart_count: int = 0
    restart_policy: str = "no"
    health: str | None = None  # healthy | unhealthy | starting | None
    health_streak: int = 0
    health_output: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    stopped_by_user: bool = False  # Docker logged a "stop" or "restart" event (the user did it)
    events: list[str] = field(default_factory=list)

    def key(self) -> list:
        """What has to change for a re-check to be worth asking the AI about."""
        return [self.state, self.exit_code, self.oom_killed, self.restart_count, self.health, self.started_at]


@dataclass
class RuleResult:
    status: str | None  # None = nothing wrong that rules can see; the AI (if any) decides
    reason: str = ""
    what_to_do: str = ""
    rule: str = ""  # machine-readable: stopped | exited_error | oom | crash_loop | health_failing | …
    ask_ai: bool = True  # False when the AI has nothing to add (stopped, never started)


def _exit_text(code: int | None) -> str:
    if code is None:
        return "an unknown code"
    if code in SIGNALS:
        return f"code {code} ({SIGNALS[code]})"
    return f"code {code}"


def evaluate(f: Facts, previous: Facts | None) -> RuleResult:
    restarts_since = (f.restart_count - previous.restart_count) if previous else 0
    if f.state in ("exited", "dead"):
        if f.oom_killed:
            return RuleResult("critical", f"{f.name} stopped because it ran out of memory.",
                              "Raise its memory limit, or find out what's using the memory.", "oom", ask_ai=True)
        if f.state == "dead":
            return RuleResult("critical", "Docker couldn't stop or remove it cleanly (state: dead).",
                              "Remove and recreate the container.", "dead")
        if f.exit_code == 0:
            return RuleResult("stopped", "It finished or was stopped normally (exit code 0).", "", "stopped", ask_ai=False)
        if f.stopped_by_user:
            return RuleResult("stopped", "Someone stopped it (docker stop or a compose down).", "",
                              "stopped", ask_ai=False)
        if f.exit_code == 143 and not f.events:
            # SIGTERM is how a normal stop ends; without Docker's event log we can't tell more.
            return RuleResult("stopped", "It was stopped (SIGTERM).", "", "stopped", ask_ai=False)
        return RuleResult("critical", f"It stopped with {_exit_text(f.exit_code)}.",
                          "Look at its last log lines for the error, fix it, then start it again.", "exited_error")
    if f.state == "created":
        return RuleResult("stopped", "It was created but never started.", "", "never_started", ask_ai=False)
    if f.state == "paused":
        return RuleResult("stopped", "It's paused.", "Unpause it when you want it running again.", "paused", ask_ai=False)
    if f.state == "restarting" or restarts_since >= 2:
        if f.oom_killed:
            return RuleResult("critical", f"It keeps running out of memory and being restarted ({f.restart_count} restarts so far).",
                              "Raise its memory limit. Restarting it again won't help.", "oom_loop")
        return RuleResult("critical", f"It keeps crashing and restarting ({f.restart_count} restarts so far).",
                          "Look at its last log lines for the error. Docker is already restarting it.", "crash_loop")
    if f.state == "running":
        if f.health == "unhealthy":
            detail = f" Its health check says: {f.health_output.strip()[:200]}" if f.health_output.strip() else ""
            return RuleResult("unhealthy", f"It's failing its health check ({f.health_streak} times in a row).{detail}",
                              "Look at why the health check fails; a restart may help if it's stuck.", "health_failing")
        if restarts_since == 1 and not f.stopped_by_user:
            return RuleResult("degraded", "It crashed once since the last check and Docker restarted it.",
                              "If it happens again, look at its log lines from just before the restart.", "crashed_once")
        return RuleResult(None)
    return RuleResult("unknown", f"Docker reports an unusual state: {f.state}.", "", "odd_state", ask_ai=False)


def worse(a: str, b: str) -> str:
    return a if SEVERITY.get(a, 1) >= SEVERITY.get(b, 1) else b
