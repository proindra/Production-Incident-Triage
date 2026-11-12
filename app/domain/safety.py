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
_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b"),
    re.compile(r"(?i)\bdisregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b"),
    re.compile(r"(?i)\b(?:reveal|print|show)\s+(?:the\s+)?(?:system|developer)\s+prompt\b"),
    re.compile(r"(?i)\b(?:bypass|disable|override)\s+(?:the\s+)?(?:safety|guardrails?)\b"),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:the\s+)?(?:system|developer|admin)\b"),
)
_TOXICITY_PATTERNS = (
    re.compile(r"(?i)\bkill\s+yourself\b"),
    re.compile(r"(?i)\bi\s+will\s+(?:hurt|harm|kill)\s+you\b"),
    re.compile(r"(?i)\byou\s+are\s+(?:worthless|a\s+moron|an\s+idiot)\b"),
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


def inspect_untrusted_text(value: Any) -> list[str]:
    """Return local guard flags for prompt-injection and abusive text."""
    strings = list(_strings(value))
    flags: list[str] = []
    if any(pattern.search(text) for pattern in _INJECTION_PATTERNS for text in strings):
        flags.append("prompt_injection_detected")
    if any(pattern.search(text) for pattern in _TOXICITY_PATTERNS for text in strings):
        flags.append("toxic_input_detected")
    return flags


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def contains_toxicity(value: str) -> bool:
    return any(pattern.search(value) for pattern in _TOXICITY_PATTERNS)
