"""Mask obvious secrets in log lines before they're stored, shown, sent to the AI or notified.

Deliberately simple and conservative: it masks the value after well-known key names, bearer
tokens, passwords inside URLs, and a few unmistakable key formats. It can't find every secret,
so the README still says not to log secrets; this is the safety net.
"""

from __future__ import annotations

import re

MASK = "[redacted]"

_KEYS = r"(?:password|passwd|pwd|pass|secret|client_secret|token|access_token|refresh_token|auth_token|" \
        r"api[_-]?key|apikey|x-api-key|private[_-]?key|session[_-]?id|cookie|set-cookie)"

PATTERNS: list[tuple[re.Pattern, str]] = [
    # key=value, key: value, "key": "value"  (value up to a space, quote, comma, & or ;)
    (re.compile(r'(?i)(["\']?\b' + _KEYS + r'\b["\']?\s*[=:]\s*["\']?)([^\s"\'&;,}]+)'), r"\1" + MASK),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})"), r"\1 " + MASK),
    (re.compile(r"(?i)(\bauthorization\s*[:=]\s*\w+\s+)(\S+)"), r"\1" + MASK),
    # user:password@ in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)"), r"\1" + MASK + r"\3"),
    # Well-known key shapes
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),                       # AWS access key id
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),             # GitHub tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), MASK),                  # OpenAI-style keys
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b"), MASK),            # Slack tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),  # JWTs
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL), MASK),
]


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text
