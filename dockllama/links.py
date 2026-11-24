"""Remove web links and web addresses from text the AI wrote.

Everything the AI writes (check summaries, what to do, restart reasons, the daily summary) is
passed through strip_links() before it's stored, shown or sent in a notification. DockLlama
never needs the AI to hand out links, so a link in an answer is at best noise and at worst a
way to get someone to click somewhere or to smuggle data out in a URL. This applies to every
model, not just Qwen-based ones.

What counts as a link: anything with a scheme (https://, ftp://, ...), www. addresses,
markdown links, email addresses, and bare names ending in a common web domain (example.com, files.cn). File
names such as config.yml, app.py or entrypoint.sh are left alone, and so are plain IP
addresses, which are useful when a container can't reach another service.
"""

from __future__ import annotations

import re

REMOVED = "[link removed]"

# Domain endings that are almost never file names or code (no .sh, .py, .info, .io etc.).
_TLDS = ("com|net|org|cn|ai|co|dev|xyz|top|site|online|ru|tk|ly|gl|us|uk|de|me|biz|club|shop|"
         "cloud|tech|store|live|icu|vip|work|click")
_END = r"[^\s<>\"'()\[\]{}]*[^\s<>\"'()\[\]{}.,;:!?]"  # the rest of the address, minus trailing punctuation

_MARKDOWN = re.compile(r"\[([^\]\n]{0,200})\]\([^)\s]*\)")
_PATTERNS = [
    re.compile(r"(?i)[\w.+\-]+@(?:[a-z0-9\-]+\.)+[a-z]{2,}\b"),                # email addresses
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://" + r"(?:" + _END + r")?"),           # https://..., ftp://...
    re.compile(r"(?i)\bwww\.[a-z0-9\-]+" + r"(?:" + _END + r")?"),                     # www.example...
    re.compile(r"(?i)(?<![\w@./-])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+(?:" + _TLDS + r")\b"
               r"(?:[/:?#]" + _END + r")?"),                                 # example.com/path
]


def strip_links(text: str) -> str:
    if not text:
        return text
    text = _MARKDOWN.sub(lambda m: m.group(1), text)  # keep the words, drop the target
    for p in _PATTERNS:
        text = p.sub(REMOVED, text)
    return text
