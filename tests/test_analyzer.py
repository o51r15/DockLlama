from dockllama import analyzer
from dockllama.redact import redact


def test_levels_in_common_formats():
    cases = {
        "2026-11-24 03:00:00.123 UTC [27] LOG:  checkpoint starting: time": "info",
        "2026-11-24 03:00:00.123 UTC [27] FATAL:  the database system is shutting down": "error",
        "INFO\tserver started": "info",
        "2026-11-24 03:00:00 - app - WARNING - slow request": "warn",
        '{"level":"error","msg":"db down"}': "error",
        '{"severity":"INFO","message":"ok"}': "info",
        "time=2026 level=warn msg=retrying": "warn",
        "2026/11/24 03:00:00 [error] 29#29: upstream timed out": "error",
        "Traceback (most recent call last):": "error",
        "panic: runtime error: invalid memory address": "error",
        "ValueError: bad thing": "error",
        "E1124 03:00:00.000000 1 reflector.go:1] failed": "error",
        "GET /health 200 2ms": "unknown",
    }
    for line, want in cases.items():
        assert analyzer.detect_level(line) == want, line


def test_routine_errors_dont_block_recovery():
    # karakeep_chrome in v1: routine errors at the end made the summary say "no recovery"
    lines = ["INFO started"] + ["INFO page fetched"] * 10 + ["ERROR dbus connection failed"] * 3
    s = analyzer.analyze(lines, known_patterns=[{"pattern": "dbus", "tag": "[ROUTINE: chrome dbus noise]"}])
    assert s.real_errors == 0 and s.routine["error"] == 3 and s.recovery == "none"
    prompt = analyzer.build_prompt("karakeep_chrome", s, facts_line="running", resources="N/A", window="10 minutes")
    assert "3 (3 routine) ERROR" in prompt and "Recovery: N/A (all errors and warnings are routine)" in prompt


def test_recovery_states():
    errs = ["ERROR upstream refused"] * 3
    assert analyzer.analyze(errs + ["INFO ok"] * 25).recovery == "yes"
    assert analyzer.analyze(["INFO ok"] * 10 + errs).recovery == "no"
    assert analyzer.analyze(["INFO ok"] * 10 + errs + ["INFO ok"] * 2).recovery == "unclear"
    assert analyzer.analyze(["INFO ok"] * 5).recovery == "none"


def test_dedup_ignore_and_prompt_layout():
    lines = ["INFO boot", "ERROR conn 10.0.0.1:5432 refused after 31ms", "ERROR conn 10.0.0.2:5432 refused after 45ms",
             "DEBUG noisy poll", "WARN retrying", "INFO ok"] + ["INFO ok"] * 6
    s = analyzer.analyze(lines, ignore_patterns=["noisy poll", "([bad regex"])
    assert s.total == 11 and len([p for p in s.patterns if p.level == "error"]) == 1
    assert s.patterns[0].count == 2 and (s.patterns[0].first, s.patterns[0].last) == (2, 3)
    p = analyzer.build_prompt("web", s, facts_line="running for 3 days, 0 restarts", resources="CPU 2% | RAM 10%",
                              window="10 minutes", notes="A web app. Brief DB reconnects are normal.")
    for piece in ["Container: web", "Container-Specific Context: A web app.", "Deduplicated Errors:",
                  '(x2, lines 2-3)', "Deduplicated Warnings:", "Recovery: YES", "Recent Tail (last 11 lines):", "[11/11] INFO ok"]:
        assert piece in p, piece


def test_postgres_restart_is_recognised():
    lines = ["LOG: received fast shutdown request", "FATAL: terminating connection due to administrator command",
             "LOG: database system is shut down", "LOG: database system is ready to accept connections",
             "LOG: checkpoint starting"]
    s = analyzer.analyze(lines)
    assert s.restart.startswith("Detected") and s.patterns[0].context == "During shutdown sequence"


def test_no_logs():
    s = analyzer.analyze([])
    assert "no new log lines" in analyzer.build_prompt("x", s, facts_line="running", resources="N/A", window="10 minutes")


def test_redaction():
    assert redact("db password=hunter2 ok") == "db password=[redacted] ok"
    assert redact("postgres://u:pw@h/db") == "postgres://u:[redacted]@h/db"
    assert redact('{"apiKey": "abcdef123456"}') == '{"apiKey": "[redacted]"}'
    assert redact('level=info msg="token refreshed"') == 'level=info msg="token refreshed"'
    assert redact("tokens processed: 4096") == "tokens processed: 4096"
