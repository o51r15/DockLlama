"""The 8 test scenarios a model is tested on, and how each answer is scored.

Copied from v1's BENCHMARK_SCENARIOS (the same set ollama_bench3.py --apps dockllama replays),
with the plan's rubric fix: S2 (out-of-memory loop) expects no restart, because Docker is already
restarting it and a restart doesn't fix running out of memory. S8 already expects "degraded".
Scoring is v1's _score_scenario: status 30, score range 30, action 20, restart 10, summary 10.
"""

from __future__ import annotations

SCENARIOS: list[dict] = [
    {
        "id": "S1",
        "name": "Clean Healthy Container",
        "tier": 1,
        "summary": (
            "Container: test-healthy-postgres\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 2.1% | RAM 18.4%\n"
            "Log Severities: 0 ERROR, 0 WARN, 12 INFO\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] LOG: checkpoint starting: time\n"
            "[2/10] LOG: checkpoint complete: wrote 42 buffers (0.3%)\n"
            '[3/10] LOG: automatic analyze of table "public.users" system usage is 1%\n'
            "[4/10] LOG: checkpoint starting: time\n"
            "[5/10] LOG: checkpoint complete: wrote 18 buffers (0.1%)\n"
            "[6/10] LOG: connection received: host=172.19.0.3 port=54210\n"
            "[7/10] LOG: connection authorized: user=app database=prod\n"
            "[8/10] LOG: disconnection: session time: 0:00:02.104\n"
            "[9/10] LOG: checkpoint starting: time\n"
            "[10/10] LOG: checkpoint complete: wrote 5 buffers (0.0%)"
        ),
        "expect_status": "healthy",
        "score_min": 85,
        "score_max": 100,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S2",
        "name": "OOM Crash Loop",
        "tier": 1,
        "summary": (
            "Container: test-failing-worker\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 99.8% | RAM 97.2%\n"
            "Log Severities: 14 ERROR, 3 WARN, 0 INFO\n"
            "Deduplicated Errors:\n"
            '  "Out of memory: Killed process 1842 (node)" (x6)\n'
            '  "Container exceeded memory limit, OOM killed" (x4)\n'
            '  "FATAL: the database system is shutting down" (x2)\n'
            '  "panic: runtime error: invalid memory address" (x2)\n'
            "Recovery: No recovery detected - errors continue through most recent lines.\n"
            "Restart Sequence: Detected - container restarting repeatedly.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] ERROR: Out of memory: Killed process 1842 (node)\n"
            "[2/10] ERROR: Container exceeded memory limit, OOM killed\n"
            "[3/10] WARN: Container restart count: 5 in last 10 minutes\n"
            "[4/10] ERROR: FATAL: the database system is shutting down\n"
            "[5/10] ERROR: Out of memory: Killed process 1843 (node)\n"
            "[6/10] ERROR: Container exceeded memory limit, OOM killed\n"
            "[7/10] ERROR: panic: runtime error: invalid memory address\n"
            "[8/10] WARN: process exited with code 137 (SIGKILL)\n"
            "[9/10] ERROR: Out of memory: Killed process 1844 (node)\n"
            "[10/10] ERROR: Container exceeded memory limit, OOM killed"
        ),
        "expect_status": "critical",
        "score_min": 0,
        "score_max": 15,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S3",
        "name": "Recovered After Errors",
        "tier": 2,
        "summary": (
            "Container: test-api-gateway\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 12.3% | RAM 45.1%\n"
            "Log Severities: 5 ERROR, 2 WARN, 35 INFO\n"
            "Deduplicated Errors:\n"
            '  "Connection refused to upstream backend:8080" (x3, lines 2-8)\n'
            '  "Failed health check for service payments" (x2, lines 5-9)\n'
            "Recovery: YES - clean operation from line 15 onward, no errors in last 35 lines.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: GET /api/users 200 12ms\n"
            "[2/10] INFO: POST /api/orders 201 45ms\n"
            "[3/10] INFO: GET /api/health 200 2ms\n"
            "[4/10] INFO: GET /api/products 200 18ms\n"
            "[5/10] INFO: POST /api/payments 201 89ms\n"
            "[6/10] INFO: GET /api/users 200 11ms\n"
            "[7/10] INFO: GET /api/health 200 1ms\n"
            "[8/10] INFO: POST /api/orders 201 52ms\n"
            "[9/10] INFO: GET /api/products 200 15ms\n"
            "[10/10] INFO: GET /api/health 200 2ms"
        ),
        "expect_status": "healthy",
        "score_min": 80,
        "score_max": 100,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S4",
        "name": "Routine Errors (Known Patterns)",
        "tier": 2,
        "summary": (
            "Container: test-bitmagnet\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 3.5% | RAM 22.8%\n"
            "Log Severities: 18 (18 routine) ERROR, 15 (15 routine) WARN, 5 INFO\n"
            "Container-Specific Context: BitTorrent indexer. DHT errors, tracker timeouts, peer resets are normal.\n"
            "Deduplicated Errors:\n"
            '  "dial tcp 45.33.32.156:6881: i/o timeout" (x8) [ROUTINE: DHT network timeout]\n'
            '  "tracker responded with 503" (x5) [ROUTINE: tracker temporarily unavailable]\n'
            '  "peer connection reset by remote" (x5) [ROUTINE: peer disconnected]\n'
            "Recovery: N/A (all errors are routine)\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: indexed 142 new torrents in last 5m\n"
            "[2/10] WARN: torrent metadata fetch timed out [ROUTINE: slow metadata]\n"
            "[3/10] ERROR: dial tcp 91.121.59.153:6881: i/o timeout [ROUTINE: DHT network timeout]\n"
            "[4/10] INFO: DHT nodes: 2847 active\n"
            "[5/10] ERROR: tracker responded with 503 [ROUTINE: tracker temporarily unavailable]\n"
            "[6/10] WARN: DHT node unresponsive [ROUTINE: DHT churn]\n"
            "[7/10] INFO: indexed 138 new torrents in last 5m\n"
            "[8/10] ERROR: peer connection reset by remote [ROUTINE: peer disconnected]\n"
            "[9/10] INFO: processing queue: 45 pending\n"
            "[10/10] WARN: torrent metadata fetch timed out [ROUTINE: slow metadata]"
        ),
        "expect_status": "healthy",
        "score_min": 85,
        "score_max": 100,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S5",
        "name": "Degraded Performance",
        "tier": 2,
        "summary": (
            "Container: test-web-frontend\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 78.4% | RAM 71.2%\n"
            "Log Severities: 3 ERROR, 12 WARN, 40 INFO\n"
            "Deduplicated Errors:\n"
            '  "upstream response timeout (30s)" (x3)\n'
            "Deduplicated Warnings:\n"
            '  "request processing time exceeded 5s" (x8)\n'
            '  "connection pool near capacity (45/50)" (x4)\n'
            "Recovery: UNCLEAR - slow responses intermittent throughout window.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: GET /dashboard 200 3200ms\n"
            "[2/10] WARN: request processing time exceeded 5s\n"
            "[3/10] INFO: GET /api/data 200 1800ms\n"
            "[4/10] INFO: GET /static/app.js 200 12ms\n"
            "[5/10] WARN: connection pool near capacity (45/50)\n"
            "[6/10] INFO: POST /api/submit 201 4200ms\n"
            "[7/10] WARN: request processing time exceeded 5s\n"
            "[8/10] INFO: GET /api/status 200 890ms\n"
            "[9/10] INFO: GET /dashboard 200 2100ms\n"
            "[10/10] WARN: request processing time exceeded 5s"
        ),
        "expect_status": "degraded",
        "score_min": 40,
        "score_max": 70,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S6",
        "name": "Graceful Shutdown (FATAL is Normal)",
        "tier": 3,
        "summary": (
            "Container: test-postgres-replica\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 0.5% | RAM 12.3%\n"
            "Log Severities: 6 ERROR, 0 WARN, 8 INFO\n"
            "Deduplicated Errors:\n"
            '  "FATAL: the database system is shutting down" (x3, lines 8-10)\n'
            '  "FATAL: terminating connection due to administrator command" (x3, lines 8-12)\n'
            "Recovery: YES - container restarted successfully, accepting connections.\n"
            "Restart Sequence: Detected - single clean restart (not a loop).\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] LOG: received fast shutdown request\n"
            "[2/10] LOG: aborting any active transactions\n"
            "[3/10] FATAL: terminating connection due to administrator command\n"
            "[4/10] FATAL: the database system is shutting down\n"
            "[5/10] LOG: all server processes terminated; reinitializing\n"
            "[6/10] LOG: database system was shut down at 2026-07-25 22:15:03 UTC\n"
            "[7/10] LOG: database system is ready to accept connections\n"
            "[8/10] LOG: checkpoint starting: time\n"
            "[9/10] LOG: checkpoint complete: wrote 0 buffers (0.0%)\n"
            "[10/10] LOG: connection received: host=172.19.0.5 port=43210"
        ),
        "expect_status": "healthy",
        "score_min": 75,
        "score_max": 100,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S7",
        "name": "High CPU Normal Workload",
        "tier": 3,
        "summary": (
            "Container: test-plex-transcoder\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 95.2% | RAM 68.4%\n"
            "Log Severities: 0 ERROR, 0 WARN, 28 INFO\n"
            "Errors: NONE\n"
            "Recovery: N/A (no errors detected)\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: Transcoding session started for user mike - 4K HEVC to 1080p H264\n"
            "[2/10] INFO: Transcode speed: 2.1x realtime\n"
            "[3/10] INFO: Transcoding session started for user sarah - 4K HDR to 720p\n"
            "[4/10] INFO: Transcode speed: 1.8x realtime\n"
            "[5/10] INFO: Direct play session for user john\n"
            "[6/10] INFO: Transcode speed: 2.0x realtime\n"
            "[7/10] INFO: Media analysis complete: /data/movies/new_release.mkv\n"
            "[8/10] INFO: Transcode speed: 1.9x realtime\n"
            "[9/10] INFO: Transcoding session started for user alex - 1080p to 480p\n"
            "[10/10] INFO: Transcode speed: 4.2x realtime"
        ),
        "expect_status": "healthy",
        "score_min": 85,
        "score_max": 100,
        "expect_action": "none",
        "expect_restart": False,
    },
    {
        "id": "S8",
        "name": "External Dependency Failure",
        "tier": 3,
        "summary": (
            "Container: test-payment-processor\n"
            "Time Window: 15 minutes\n"
            "Resource Usage: CPU 5.3% | RAM 28.1%\n"
            "Log Severities: 10 ERROR, 8 WARN, 20 INFO\n"
            "Deduplicated Errors:\n"
            '  "HTTP 503 from https://api.stripe.com/v1/charges" (x5)\n'
            '  "HTTP 502 from https://api.stripe.com/v1/refunds" (x3)\n'
            '  "Connection timed out: api.stripe.com:443" (x2)\n'
            "Deduplicated Warnings:\n"
            '  "Retry attempt 3/5 for payment charge" (x5)\n'
            '  "Circuit breaker OPEN for stripe-api" (x3)\n'
            "Recovery: No recovery - external API still failing in most recent lines.\n"
            "Recent Tail (last 10 lines):\n"
            "[1/10] INFO: Processing payment request #98712\n"
            "[2/10] ERROR: HTTP 503 from https://api.stripe.com/v1/charges\n"
            "[3/10] WARN: Retry attempt 3/5 for payment charge\n"
            "[4/10] ERROR: Connection timed out: api.stripe.com:443\n"
            "[5/10] WARN: Circuit breaker OPEN for stripe-api\n"
            "[6/10] INFO: Queued payment #98712 for later retry\n"
            "[7/10] INFO: Processing payment request #98713\n"
            "[8/10] ERROR: HTTP 503 from https://api.stripe.com/v1/charges\n"
            "[9/10] WARN: Retry attempt 2/5 for payment charge\n"
            "[10/10] INFO: Queued payment #98713 for later retry"
        ),
        "expect_status": "degraded",
        "score_min": 30,
        "score_max": 65,
        "expect_action": "none",
        "expect_restart": False,
    },
]


def score_answer(scenario: dict, verdict: dict) -> tuple[int, list[str]]:
    """Points out of 100 for one parsed answer, and what was wrong (v1's _score_scenario)."""
    want = scenario["expect_status"]
    got = verdict["status"]
    points, notes = 0, []
    if got == want:
        points += 30
    elif want == "healthy" and got == "degraded":
        points += 12
    elif want == "critical" and got == "unhealthy":
        points += 18
    elif want == "degraded" and got in ("healthy", "unhealthy"):
        points += 10
    if got != want:
        notes.append(f"said {got}, expected {want}")
    lo, hi = scenario["score_min"], scenario["score_max"]
    score = verdict["health_score"]
    if lo <= score <= hi:
        points += 30
    else:
        if abs(score - lo) <= 10 or abs(score - hi) <= 10:
            points += 15
        notes.append(f"score {score}, expected {lo}–{hi}")
    if verdict["recommended_action"] == scenario["expect_action"]:
        points += 20
    else:
        notes.append(f"action {verdict['recommended_action']}")
    if verdict["restart_would_help"] == scenario["expect_restart"]:
        points += 10
    else:
        notes.append("wrong about whether a restart would help")
    if verdict["summary"] and len(verdict["summary"]) > 15:
        points += 10
    else:
        notes.append("summary too short")
    return points, notes
