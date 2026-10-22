"""Removes secrets and card numbers from telemetry before it is stored.

The demo services never log secrets on purpose, but real code eventually logs a whole
request by mistake. ServScope scrubs on the way in as a second line of defence, so a
mistake in one service can't leave a password sitting in the database for a week.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"
SECRET_KEYS = re.compile(
    r"pass(word)?|secret|token|api[_-]?key|authori[sz]ation|cookie|card[_-]?number|cvv|cvc",
    re.IGNORECASE,
)
# 13 to 19 digits, optionally split by spaces or dashes: the shape of a payment card number.
CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
# key=value or key: value pairs inside free text, e.g. "login failed password=hunter2"
INLINE_SECRET = re.compile(
    r"(?P<key>\b(?:pass(?:word)?|secret|token|api[_-]?key)\b\s*[=:]\s*)(?P<value>[^\s,;&]+)",
    re.IGNORECASE,
)


def scrub_text(text: str) -> str:
    text = INLINE_SECRET.sub(lambda m: m.group("key") + REDACTED, text)
    return CARD_NUMBER.sub(REDACTED, text)


def scrub_value(key: str, value: Any) -> Any:
    if SECRET_KEYS.search(key):
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return scrub_attributes(value)
    if isinstance(value, list):
        return [scrub_value(key, v) for v in value]
    return value


def scrub_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: scrub_value(k, v) for k, v in attrs.items()}
