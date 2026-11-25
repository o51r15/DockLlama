"""When a message can't be sent, say why in plain words (Apprise itself only says yes/no)."""

import http.server
import json
import threading

import pytest

from dockllama.notify import GENERIC_FAILURE, explain_failure, send_now


@pytest.fixture
def service():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            topic = str(body.get("topic", ""))
            self.send_response(int(topic) if topic.isdigit() else 200)
            self.end_headers()
            self.wfile.write(b'{"error":"no"}')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_real_sends(service):
    assert send_now(f"ntfy://127.0.0.1:{service}/ok", "t", "b") == (True, "")
    ok, why = send_now(f"ntfy://127.0.0.1:{service}/403", "t", "b")
    assert not ok and why.startswith("ntfy turned the message down (HTTP 403)") and "token" in why
    ok, why = send_now(f"ntfy://127.0.0.1:{service}/404", "t", "b")
    assert "not found" in why and "HTTP 404" in why
    ok, why = send_now(f"ntfy://127.0.0.1:{service}/429", "t", "b")
    assert "HTTP 429" in why
    ok, why = send_now("ntfy://127.0.0.1:1/x", "t", "b")
    assert why == "127.0.0.1 refused the connection. Check the address and port, and that the service is running."
    ok, why = send_now("ntfy://no-such-host.invalid/x", "t", "b")
    assert why.startswith("Couldn't find no-such-host.invalid")


@pytest.mark.parametrize("log,expect", [
    ("WARNING - A Connection error occurred sending ntfy:https://ntfy.sh notification.\\n"
     "DEBUG - Socket Exception: HTTPSConnectionPool(host='ntfy.sh', port=443): Read timed out. (read timeout=4.0)",
     "No answer from ntfy.sh in time"),
    ("DEBUG - Socket Exception: HTTPSConnectionPool(host='ntfy.sh', port=443): Max retries exceeded "
     "(Caused by SSLError(SSLCertVerificationError('certificate verify failed')))", "certificate"),
    ("DEBUG - Socket Exception: HTTPSConnectionPool(host='ntfy.sh', port=443): _ssl.c:994: The handshake operation timed out",
     "No answer from ntfy.sh in time"),
    ("WARNING - Failed to send Discord notification: Bad Gateway, error=502.", "Discord had a problem on its side (HTTP 502)"),
    ("something nobody expected", GENERIC_FAILURE),
])
def test_explanations(log, expect):
    url = "discord://id/token" if "Discord" in log else "ntfys://ntfy.sh/topic"
    assert expect in explain_failure(log, url)


def test_secrets_in_the_log_never_reach_the_message():
    log = ("WARNING - Failed to send Telegram notification to 123:SECRET-TOKEN: Unauthorized, error=401.")
    msg = explain_failure(log, "tgram://123:SECRET-TOKEN/456")
    assert "SECRET" not in msg and msg.startswith("Telegram turned the message down (HTTP 401)")
