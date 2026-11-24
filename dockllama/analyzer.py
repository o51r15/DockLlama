"""Turn a container's recent log lines into a short structured summary for the AI.

From v1's log_analyzer.py, with these changes:
- Lines tagged routine (the container's "known patterns") don't count as errors when deciding
  whether the container recovered, and come after real errors in the list. In v1 a routine
  error at the end of the window made the summary say "no recovery" (karakeep_chrome's false
  "degraded").
- The summary uses the same layout as the 8 test scenarios, so a model's test score predicts
  how it reads real summaries.
- A few more common error formats are recognised (JSON log levels, Python tracebacks, Go panics).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

LEVEL_PATTERNS = [
    # Go-style and plain prefixes: "INFO\t...", "ERROR ..."
    (re.compile(r"^(INFO|DEBUG|TRACE)\b"), "info"),
    (re.compile(r"^(WARN|WARNING)\b"), "warn"),
    (re.compile(r"^(ERROR|FATAL|PANIC|CRITICAL)\b"), "error"),
    # PostgreSQL: "2026-07-18 00:48:26.225 UTC [27] LOG:"
    (re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}.*\b(LOG|INFO|DEBUG\d?):"), "info"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}.*\bWARNING:"), "warn"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}.*\b(ERROR|FATAL|PANIC):"), "error"),
    # JSON logs: "level":"error", "severity":"ERROR"
    (re.compile(r'"(level|severity|lvl)"\s*:\s*"(error|fatal|panic|critical|err)"', re.IGNORECASE), "error"),
    (re.compile(r'"(level|severity|lvl)"\s*:\s*"(warn|warning)"', re.IGNORECASE), "warn"),
    (re.compile(r'"(level|severity|lvl)"\s*:\s*"(info|debug|trace|notice)"', re.IGNORECASE), "info"),
    # Python logging and friends: "- INFO -", "INFO:", "[INFO]"
    (re.compile(r"\b(ERROR|CRITICAL|FATAL)\s*[-:\]]"), "error"),
    (re.compile(r"\b(WARNING|WARN)\s*[-:\]]"), "warn"),
    (re.compile(r"\b(INFO|DEBUG|NOTICE)\s*[-:\]]"), "info"),
    # nginx / apache: [error], [warn]
    (re.compile(r"\[(error|crit|alert|emerg)\]", re.IGNORECASE), "error"),
    (re.compile(r"\[warn(ing)?\]", re.IGNORECASE), "warn"),
    (re.compile(r"\[(info|notice|debug)\]", re.IGNORECASE), "info"),
    # key=value: level=info
    (re.compile(r"\blevel=(error|fatal|panic|critical|err)\b", re.IGNORECASE), "error"),
    (re.compile(r"\blevel=(warn|warning)\b", re.IGNORECASE), "warn"),
    (re.compile(r"\blevel=(info|debug|trace)\b", re.IGNORECASE), "info"),
    # Crashes
    (re.compile(r"^Traceback \(most recent call last\)"), "error"),
    (re.compile(r"^panic: "), "error"),
    (re.compile(r"^[A-Za-z_.]*(Error|Exception): "), "error"),
    # One-letter Go/glog style: "E1124 03:00:00.000 ..." / "W1124 ..."
    (re.compile(r"^E\d{4} \d{2}:\d{2}:\d{2}"), "error"),
    (re.compile(r"^W\d{4} \d{2}:\d{2}:\d{2}"), "warn"),
]

SHUTDOWN_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"received.*shutdown.*request", r"shutting down", r"stopping\s+(service|server|process)",
    r"graceful\s+shutdown", r"\bSIGTERM\b|\bSIGINT\b|signal.*TERM")]
STARTUP_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"ready to accept connections", r"started?\s+(server|service|worker|listening)", r"listening\s+on",
    r"server\s+started", r"initialization\s+complete", r"starting\s+PostgreSQL", r"database system is ready",
    r"application startup complete")]
NORMALIZE_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.,\d]*\s*(UTC|Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<ID>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<ADDR>"),
    (re.compile(r"\d+"), "#"),
    (re.compile(r"\s+"), " "),
]

TAIL_LINES = 25
TAIL_LINE_CHARS = 300
MESSAGE_CHARS = 200


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def detect_level(line: str) -> str:
    for pattern, level in LEVEL_PATTERNS:
        if pattern.search(line):
            return level
    return "unknown"


def _normalize(msg: str) -> str:
    for pattern, repl in NORMALIZE_PATTERNS:
        msg = pattern.sub(repl, msg)
    return msg.strip()[:150]


@dataclass
class Pattern:
    message: str
    count: int
    first: int  # 1-based line numbers
    last: int
    level: str
    routine: bool
    context: str = ""


@dataclass
class LogSummary:
    total: int = 0
    counts: dict = field(default_factory=lambda: {"info": 0, "warn": 0, "error": 0, "unknown": 0})
    routine: dict = field(default_factory=lambda: {"warn": 0, "error": 0})
    patterns: list[Pattern] = field(default_factory=list)
    recovery: str = "none"  # none (no errors) | yes | no | unclear
    clean_after: int = 0  # lines after the last real error
    restart: str = ""
    tail: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    real_errors: int = 0  # error lines that aren't routine
    real_warnings: int = 0

    def fingerprint(self) -> str:
        return json.dumps([self.total, self.counts, [p.message for p in self.patterns[:5]]])


def _compile(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out


def analyze(lines: list[str], ignore_patterns: list[str] | None = None,
            known_patterns: list[dict] | None = None) -> LogSummary:
    """Summarise log lines (oldest first, already capped and redacted)."""
    ignores = _compile(ignore_patterns or [])
    known = []
    for kp in known_patterns or []:
        try:
            known.append((re.compile(kp["pattern"]), kp.get("tag") or "[ROUTINE: known pattern]"))
        except (re.error, KeyError, TypeError):
            continue

    s = LogSummary()
    routine_flags: list[bool] = []
    for raw in lines:
        clean = strip_ansi(raw).strip()
        if not clean or any(p.search(clean) for p in ignores):
            continue
        routine = False
        for regex, tag in known:
            if regex.search(clean):
                clean, routine = f"{clean} {tag}", True
                break
        level = detect_level(clean)
        s.lines.append(clean)
        s.levels.append(level)
        routine_flags.append(routine)
    s.total = len(s.lines)
    if not s.total:
        return s

    c = Counter(s.levels)
    s.counts = {k: c.get(k, 0) for k in ("info", "warn", "error", "unknown")}
    s.routine = {
        "warn": sum(1 for lv, r in zip(s.levels, routine_flags) if r and lv == "warn"),
        "error": sum(1 for lv, r in zip(s.levels, routine_flags) if r and lv == "error"),
    }
    s.real_errors = s.counts["error"] - s.routine["error"]
    s.real_warnings = s.counts["warn"] - s.routine["warn"]

    # Recovery: only real (non-routine) problems count.
    problem_idx = [i for i, (lv, r) in enumerate(zip(s.levels, routine_flags)) if lv in ("error", "warn") and not r]
    if not problem_idx:
        s.recovery = "none"
    else:
        last = problem_idx[-1] + 1
        s.clean_after = s.total - last
        # Recovered = a clean stretch at the end: at least 5 lines, up to 20 for long logs.
        if s.clean_after >= max(5, min(20, s.total // 3)):
            s.recovery = "yes"
        elif s.clean_after == 0:
            s.recovery = "no"
        else:
            s.recovery = "unclear"

    shutdown = next((i for i in range(s.total - 1, -1, -1) if any(p.search(s.lines[i]) for p in SHUTDOWN_PATTERNS)), None)
    startup = next((i for i in range(s.total - 1, -1, -1) if any(p.search(s.lines[i]) for p in STARTUP_PATTERNS)), None)
    if shutdown is not None and startup is not None and startup > shutdown:
        s.restart = f"Detected - shut down at line {shutdown + 1}, back up at line {startup + 1}."
    elif startup is not None and startup < s.total // 3:
        s.restart = f"Container started at line {startup + 1}."

    groups: dict[str, Pattern] = {}
    for i, (line, level, routine) in enumerate(zip(s.lines, s.levels, routine_flags)):
        if level not in ("error", "warn"):
            continue
        key = level + "|" + _normalize(line)
        if key in groups:
            g = groups[key]
            g.count += 1
            g.last = i + 1
        else:
            ctx = "During shutdown sequence" if shutdown is not None and abs(i - shutdown) <= 10 else ""
            groups[key] = Pattern(line[:MESSAGE_CHARS], 1, i + 1, i + 1, level, routine, ctx)
    s.patterns = sorted(groups.values(), key=lambda g: (g.routine, g.level != "error", -g.count))
    s.tail = s.lines[-TAIL_LINES:]
    return s


def _sev(count: int, routine: int) -> str:
    return f"{count} ({routine} routine)" if routine else str(count)


def build_prompt(name: str, summary: LogSummary, *, facts_line: str, resources: str, window: str,
                 notes: str = "", tail_lines: int = TAIL_LINES, max_patterns: int = 10) -> str:
    """The text the AI reads, in the test scenarios' layout."""
    s = summary
    parts = [f"Container: {name}", f"Time Window: {window}", f"Docker: {facts_line}", f"Resource Usage: {resources}"]
    if notes.strip():
        parts.append(f"Container-Specific Context: {notes.strip()}")
    if s.total == 0:
        parts.append("Log Severities: no new log lines in this window")
        return "\n".join(parts)
    parts.append(f"Log Severities: {_sev(s.counts['error'], s.routine['error'])} ERROR, "
                 f"{_sev(s.counts['warn'], s.routine['warn'])} WARN, {s.counts['info']} INFO, "
                 f"{s.counts['unknown']} other")
    errors = [p for p in s.patterns if p.level == "error"][:max_patterns]
    warnings = [p for p in s.patterns if p.level == "warn"][:max(3, max_patterns // 2)]
    for title, group in (("Deduplicated Errors:", errors), ("Deduplicated Warnings:", warnings)):
        if group:
            parts.append(title)
            for p in group:
                where = f"line {p.first}" if p.first == p.last else f"lines {p.first}-{p.last}"
                ctx = f" [{p.context}]" if p.context else ""
                parts.append(f'  "{p.message}" (x{p.count}, {where}){ctx}')
    if s.recovery == "none":
        parts.append("Errors: NONE" if not (s.counts["error"] or s.counts["warn"])
                     else "Recovery: N/A (all errors and warnings are routine)")
    elif s.recovery == "yes":
        parts.append(f"Recovery: YES - {s.clean_after} clean lines after the last error, none in the most recent lines.")
    elif s.recovery == "no":
        parts.append("Recovery: NO - errors continue through the most recent lines.")
    else:
        parts.append(f"Recovery: UNCLEAR - {s.clean_after} lines after the last error, with problems among the recent lines.")
    if s.restart:
        parts.append(f"Restart Sequence: {s.restart}")
    tail = s.tail[-tail_lines:]
    if tail:
        parts.append(f"Recent Tail (last {len(tail)} lines):")
        for i, line in enumerate(tail, 1):
            parts.append(f"[{i}/{len(tail)}] {line[:TAIL_LINE_CHARS]}")
    return "\n".join(parts)
