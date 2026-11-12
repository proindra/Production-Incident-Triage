"""Small helpers for preventing accidental exposure of secrets."""

from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*(['\"]?)[^\s,'\"}]+"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s+(?:is\s+)?(['\"]?)[^\s,'\"}]+"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s/]+(@)"),
    re.compile(r"(?i)\b(authorization:\s*bearer\s+)[A-Za-z0-9._~+/-]+=*"),
)


def redact_secrets(value: Any) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        if "://" in pattern.pattern:
            text = pattern.sub(r"\1[REDACTED]\2", text)
        elif "authorization" in pattern.pattern:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub(r"\1=[REDACTED]", text)
    return text
