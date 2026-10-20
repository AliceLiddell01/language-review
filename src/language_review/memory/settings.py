from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from language_review.errors import ConfigurationError


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = False
    dsn_env: str = "LANGUAGE_REVIEW_MEMORY_DSN"
    projects: dict[str, str] = field(default_factory=dict)
    recall_mode: str = "hybrid"
    knowledge_dataset: str = "operational"
    embedding_endpoint: str = "http://127.0.0.1:11434"
    embedding_model: str = ""
    embedding_revision: str = ""
    embedding_dimensions: int = 768
    timeout_seconds: int = 60
    context_chars: int = 4000
    max_attempts: int = 3

    def public(self) -> dict[str, Any]:
        # Идентичности проектов и расположение БД не публикуются в check/review.
        return {key: value for key, value in asdict(self).items() if key != "projects"}


def project_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ConfigurationError("Идентичность проекта должна быть UUID.") from exc


def validate_memory(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("memory должна быть таблицей.")
    defaults = asdict(MemorySettings())
    if set(raw) - set(defaults):
        raise ConfigurationError("Неизвестный параметр memory.")
    result = dict(raw)
    for key, value in raw.items():
        expected = defaults[key]
        if type(value) is not type(expected):
            raise ConfigurationError(f"Некорректный тип memory.{key}.")
        if key == "enabled":
            continue
        if key == "projects":
            result[key] = {}
            for root, identifier in value.items():
                if not re.fullmatch(r"root-\d{4}", root) or not isinstance(identifier, str):
                    raise ConfigurationError("memory.projects требует root-NNNN = UUID.")
                result[key][root] = project_uuid(identifier)
        elif isinstance(value, int):
            bounds = {
                "embedding_dimensions": (1, 2000),
                "timeout_seconds": (1, 120),
                "context_chars": (256, 16000),
                "max_attempts": (1, 10),
            }
            low, high = bounds[key]
            if not low <= value <= high:
                raise ConfigurationError(f"memory.{key} вне допустимых границ.")
        elif key == "dsn_env":
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value):
                raise ConfigurationError("memory.dsn_env должна быть именем переменной окружения.")
        elif key == "recall_mode":
            if value not in {"structured", "hybrid"}:
                raise ConfigurationError("memory.recall_mode: structured или hybrid.")
        elif key == "knowledge_dataset":
            if value not in {"operational", "synthetic"}:
                raise ConfigurationError("memory.knowledge_dataset: operational или synthetic.")
        elif key == "embedding_endpoint":
            try:
                url = urlsplit(value)
                port = url.port
            except ValueError as exc:
                raise ConfigurationError("Некорректный embedding endpoint.") from exc
            if (
                url.scheme != "http"
                or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username
                or url.password
                or url.query
                or url.fragment
                or (port is not None and not 1 <= port <= 65535)
            ):
                raise ConfigurationError("Embedding endpoint должен быть локальным HTTP URL.")
        elif key == "embedding_revision":
            if value and not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ConfigurationError("memory.embedding_revision требует SHA-256 модели Ollama.")
        elif key == "embedding_model":
            if len(value) > 200 or any(char.isspace() for char in value):
                raise ConfigurationError("Некорректное имя embedding-модели.")
    return result
