"""Web page checks: is a container's own web page still answering?

Catches the "frozen but still running" case: the process is up, Docker sees nothing wrong, the
logs have gone quiet, but the app no longer answers.

Only local addresses are allowed (this machine through host.docker.internal, a LAN IP, a container
name, a .lan/.local name). A public address goes through Cloudflare, a reverse proxy or a CDN,
which can answer with their own page while the app behind them is frozen, so checking it would
say "fine" when it isn't. That's exactly why uptime monitors that check public URLs miss this.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT = 10.0
FAILS_NEEDED = 3  # checks in a row before it counts as a problem
MAX_BODY = 1_000_000
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")
LOCAL_NAMES = ("localhost", "host.docker.internal")
LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # home and office networks
    "127.0.0.0/8", "169.254.0.0/16",                     # this machine, link-local
    "100.64.0.0/10",                                     # Tailscale and other carrier-grade NAT
    "::1/128", "fc00::/7", "fe80::/10"))

TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests swap in a fake

PUBLIC_EXPLAINED = (
    "That's a public address. DockLlama only checks local addresses, because a public address goes "
    "through Cloudflare or a proxy, which can answer with its own page while the app is frozen. Use "
    "this machine's local IP and the container's port, e.g. http://192.168.1.20:8080.")


def address_problem(url: str) -> str | None:
    """Why this address can't be used, in plain words, or None when it's fine."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return "That doesn't look like a web address. Try something like http://192.168.1.20:8080."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Start the address with http:// or https://, e.g. http://192.168.1.20:8080."
    host = parts.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if any(ip in net for net in LOCAL_NETWORKS if net.version == ip.version):
            return None
        return PUBLIC_EXPLAINED
    if host in LOCAL_NAMES or "." not in host or host.endswith(LOCAL_SUFFIXES):
        return None
    return PUBLIC_EXPLAINED


def suggestions(inspect: dict, machine_url: str | None) -> list[str]:
    """Addresses to offer, from the container's published ports. For this machine DockLlama
    reaches the host as host.docker.internal; for a Remote, by that machine's address."""
    host = "host.docker.internal"
    if machine_url:
        host = urlsplit(machine_url).hostname or host
    ports = ((inspect.get("NetworkSettings") or {}).get("Ports")
             or (inspect.get("HostConfig") or {}).get("PortBindings") or {})
    out = []
    for container_port, bindings in sorted(ports.items(), key=lambda kv: int(kv[0].split("/")[0])):
        if not container_port.endswith("/tcp") or not bindings:
            continue
        for b in bindings:
            if b.get("HostIp") in ("127.0.0.1", "::1"):
                continue  # only reachable from the host itself
            scheme = "https" if container_port.split("/")[0] in ("443", "8443") else "http"
            url = f"{scheme}://{host}:{b.get('HostPort')}"
            if b.get("HostPort") and url not in out:
                out.append(url)
    return out


async def check(url: str, word: str = "") -> tuple[bool, str]:
    """(answering, what happened in plain words). Any answer below HTTP 500 counts as answering
    (a login page or "not found" still means the app is alive), unless a word is required."""
    if address_problem(url):
        return False, "The address isn't a local one."
    word = (word or "").strip().lower()
    # trust_env=False: never send a local check through a proxy from the environment.
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False, follow_redirects=False, trust_env=False,
                                 transport=TEST_TRANSPORT) as client:
        target = url
        try:
            for _ in range(4):
                resp = await client.get(target, headers={"User-Agent": "DockLlama web check"})
                location = resp.headers.get("location")
                # Only follow a redirect that stays on the same local address, and only when a word
                # has to be found: the app answering with a redirect already shows it's alive.
                if word and resp.is_redirect and location:
                    nxt = urljoin(target, location)
                    if urlsplit(nxt).netloc == urlsplit(url).netloc:
                        target = nxt
                        continue
                break
        except httpx.TimeoutException:
            return False, f"No answer within {int(TIMEOUT)} seconds."
        except httpx.ConnectError as exc:
            if "refused" in str(exc).lower():
                return False, "Nothing is answering at that address (connection refused)."
            return False, "Couldn't connect to that address."
        except httpx.HTTPError as exc:
            return False, f"The connection failed ({type(exc).__name__})."
        if resp.status_code >= 500:
            return False, f"It answered with an error (HTTP {resp.status_code})."
        if word and word not in resp.text[:MAX_BODY].lower():
            return False, f"The page answered but didn't contain \"{word}\"."
        return True, f"It answered (HTTP {resp.status_code})."
