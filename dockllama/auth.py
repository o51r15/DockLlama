"""Password hashing, sessions and login throttling.

- Passwords: scrypt (stdlib), stored as "scrypt$n$r$p$salt$hash".
- Sessions: random token in an HttpOnly, SameSite=Strict cookie; only its
  sha256 is stored, so a leaked database doesn't hand out live sessions.
- Throttling: after 5 failed logins from one address, that address waits
  30 seconds (doubling up to 15 minutes). Kept in memory; a restart resets it.
"""

from __future__ import annotations

import base64
import os
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

from . import settings

COOKIE_NAME = "dockllama_session"
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 8

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


# --- passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM
    )
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_new_password(password: str) -> str | None:
    """Return a plain-language problem with a new password, or None if it's fine."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 1024:
        return "That password is too long."
    return None


def set_password(conn: sqlite3.Connection, password: str) -> None:
    """Store a new password and sign out every existing session."""
    settings.set(conn, "auth.password_hash", hash_password(password))
    conn.execute("DELETE FROM sessions")


def login_off_by_compose() -> bool:
    """Sign-in can only be turned off in the compose file (DOCKLLAMA_LOGIN=off), never from the
    web page: someone who can open the page shouldn't be able to switch the lock off."""
    return os.environ.get("DOCKLLAMA_LOGIN", "").strip().lower() in ("off", "no", "false", "0", "disabled")


def password_is_set(conn: sqlite3.Connection) -> bool:
    return bool(settings.get(conn, "auth.password_hash"))


# --- sessions ---------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_agent: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token_hash, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?)",
        (_token_hash(token), now, now + SESSION_DAYS * 86400, (user_agent or "")[:200]),
    )
    return token


def session_valid(conn: sqlite3.Connection, token: str | None) -> bool:
    """True if the token is a live session. Extends it when it's over a day old."""
    if not token:
        return False
    now = time.time()
    row = conn.execute(
        "SELECT expires_at FROM sessions WHERE token_hash = ?", (_token_hash(token),)
    ).fetchone()
    if row is None or row["expires_at"] < now:
        return False
    new_expiry = now + SESSION_DAYS * 86400
    if new_expiry - row["expires_at"] > 86400:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?", (new_expiry, _token_hash(token))
        )
    return True


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


# --- login throttling -------------------------------------------------------


class LoginThrottle:
    FREE_ATTEMPTS = 5
    BASE_DELAY = 30.0
    MAX_DELAY = 900.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float]] = {}  # addr -> (failures, locked_until)

    def wait_seconds(self, addr: str) -> int:
        with self._lock:
            _, until = self._state.get(addr, (0, 0.0))
            return max(0, int(until - time.time() + 0.999))

    def failed(self, addr: str) -> None:
        with self._lock:
            failures, _ = self._state.get(addr, (0, 0.0))
            failures += 1
            until = 0.0
            if failures >= self.FREE_ATTEMPTS:
                delay = min(self.MAX_DELAY, self.BASE_DELAY * 2 ** (failures - self.FREE_ATTEMPTS))
                until = time.time() + delay
            self._state[addr] = (failures, until)

    def succeeded(self, addr: str) -> None:
        with self._lock:
            self._state.pop(addr, None)
