"""Машинные секреты не должны попадать в память и повторный LLM-контекст."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SENSITIVE = re.compile(
    r"(?i)(?:\b(?:password|passwd|secret|token|private[_-]?key|api[_-]?key|access[_-]?token|authorization)\b"
    r"\s*[:=]|\bBearer\s+\S+|\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|[a-z][a-z0-9+.-]*://[^\s/]*:[^\s/]*@"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/]|(?:/home/|/Users/|/tmp/))"
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def safe_text(text: str) -> str:
    # Удаляется весь чувствительный скаляр, а не только угаданный фрагмент токена.
    if _SENSITIVE.search(text):
        return "[REDACTED]"
    return "".join(char for char in text if char in "\n\t" or ord(char) >= 32)


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value
