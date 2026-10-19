"""Ограниченный JSON, схемы и канонические отпечатки оценочных артефактов."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from language_review.errors import InputError
from language_review.review_verify import _invalid_constant, _unique_objects

MAX_BYTES = 32 * 1024 * 1024


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("size_limit")
        value = json.loads(raw, object_pairs_hook=_unique_objects, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("object_required")
        return value
    except (OSError, ValueError, RecursionError) as exc:
        raise InputError("Некорректный или недоступный JSON оценочного контура.") from exc


@lru_cache
def schema(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        resources.files(__package__).joinpath("schemas", f"{name}.schema.json").read_text("utf-8")
    )
    return value


def validate(value: dict[str, Any], name: str) -> None:
    try:
        Draft202012Validator(schema(name)).validate(value)
    except (ValidationError, RecursionError) as exc:
        # Ошибка не печатает содержимое пользовательского документа или локальный путь.
        raise InputError(f"Нарушен контракт {name}.") from exc


def save_new(path: Path, value: Any) -> None:
    """Создать артефакт без перезаписи источника, корпуса или предыдущего запуска."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value))
