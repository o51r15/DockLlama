"""Docker HEALTHCHECK for both modes: the app (port 8556) or DockLlama Remote (port 8557)."""

import os
import sys
import urllib.request

if os.environ.get("DOCKLLAMA_REMOTE_TOKEN"):
    url = f"http://127.0.0.1:{os.environ.get('DOCKLLAMA_REMOTE_PORT', '8557')}/remote/v1/health"
else:
    url = f"http://127.0.0.1:{os.environ.get('DOCKLLAMA_PORT', '8556')}/api/health"
try:
    urllib.request.urlopen(url, timeout=4)
except Exception:  # noqa: BLE001
    sys.exit(1)
