"""Повторная read-only сверка версии и точных координат сохранённого ревью."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from language_review.check_report import (
    SCHEMA as CHECK_SCHEMA,
)
from language_review.check_report import (
    SCHEMA_VERSION as CHECK_SCHEMA_VERSION,
)
from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.review_report import SCHEMA

MAX_VERIFY_BYTES = 32 * 1024 * 1024


def _read(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError("not_regular_file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_VERIFY_BYTES + 1)
    if len(raw) > MAX_VERIFY_BYTES:
        raise ValueError("verification_size_limit")
    return raw


def _target(root: Path, relative: str, kind: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or PureWindowsPath(relative).drive
        or ".." in path.parts
        or "\\" in relative
        or ":" in relative
    ):
        raise ValueError("unsafe_relative_path")
    if root.is_symlink() or root.is_junction():
        raise ValueError("symbolic_link_not_followed")
    base = root.resolve(strict=True)
    if kind == "file":
        if relative != base.name:
            raise ValueError("file_root_name_mismatch")
        return base
    if kind != "directory" or not base.is_dir():
        raise ValueError("unsupported_root")
    target = base
    for part in path.parts:
        target = target / part
        if target.is_symlink() or target.is_junction():
            raise ValueError("symbolic_link_not_followed")
    target = target.resolve(strict=True)
    if not target.is_relative_to(base):
        raise ValueError("outside_root")
    return target


def _verify_quote(source: SourceMap, location: Any, quote: Any) -> None:
    if not isinstance(quote, str):
        raise ValueError("quote_unavailable")
    if not isinstance(location, dict) or set(location) != {"start", "end"}:
        raise ValueError("invalid_coordinate_structure")
    first, last = location["start"], location["end"]
    fields = {"byte_offset", "line", "column", "utf16_column"}
    if any(
        not isinstance(position, dict)
        or set(position) != fields
        or any(type(value) is not int for value in position.values())
        for position in (first, last)
    ):
        raise ValueError("invalid_coordinate_structure")
    start, end = first["byte_offset"], last["byte_offset"]
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(source.raw):
        raise ValueError("invalid_byte_range")
    if source.raw[start:end].decode(source.encoding) != quote:
        raise ValueError("quote_mismatch")
    # Восстанавливаем позиции только из точного байтового префикса, без поиска цитаты.
    prefix = source.position(0).byte_offset
    if start < prefix:
        raise ValueError("invalid_byte_range")
    char_start = len(source.raw[prefix:start].decode(source.encoding))
    char_end = len(source.raw[prefix:end].decode(source.encoding))
    if source.range_dict(char_start, char_end) != location:
        raise ValueError("coordinate_mismatch")


def load_review(report_path: Path) -> dict[str, Any]:
    try:
        report = json.loads(_read(report_path))
        if (
            not isinstance(report, dict)
            or report["schema"] != SCHEMA
            or report["schema_version"] != "1.0"
        ):
            raise ValueError("unsupported_review_schema")
        analysis = report.get("analysis")
        if (
            not isinstance(analysis, dict)
            or analysis.get("schema") != CHECK_SCHEMA
            or analysis.get("schema_version") != CHECK_SCHEMA_VERSION
            or not isinstance(analysis.get("documents"), list)
            or not isinstance(report.get("items"), list)
        ):
            raise ValueError("invalid_review_structure")
        return report
    except (OSError, ValueError, LookupError, TypeError) as exc:
        raise InputError("Некорректный или недоступный review-отчёт.") from exc


def verify_review(report_path: Path, roots: dict[str, Path]) -> dict[str, Any]:
    try:
        report = load_review(report_path)
        analysis = report["analysis"]
        root_kinds = {root["id"]: root["kind"] for root in analysis["roots"]}
        documents, items = analysis["documents"], report["items"]
        if not isinstance(documents, list) or not isinstance(items, list):
            raise ValueError("invalid_review_structure")
        known = {document["id"] for document in documents}
        if len(known) != len(documents) or len({item["id"] for item in items}) != len(items):
            raise ValueError("duplicate_identity")
        if any(item["document_id"] not in known for item in items):
            raise ValueError("unknown_document")
        results = []
        for document in documents:
            identifier = document["id"]
            state, reason = "unverified", None
            try:
                if document["source_sha256"] is None:
                    raise ValueError("source_hash_unavailable")
                root = roots.get(document["root"])
                if root is None:
                    raise ValueError("root_not_supplied")
                target = _target(root, document["path"], root_kinds[document["root"]])
                raw = _read(target)
                if hashlib.sha256(raw).hexdigest() != document["source_sha256"]:
                    state, reason = "stale", "source_sha256_mismatch"
                else:
                    encoding = document["encoding"]
                    if encoding not in {
                        "utf-8",
                        "utf-16-le",
                        "utf-16-be",
                        "utf-32-le",
                        "utf-32-be",
                        "cp1251",
                        "windows-1251",
                        "ascii",
                    }:
                        raise ValueError("unsupported_encoding")
                    source = SourceMap.create(raw, encoding, document["bom"])
                    expected = {finding["id"] for finding in document["findings"]}
                    actual = [item for item in items if item["document_id"] == identifier]
                    if expected != {item["id"] for item in actual}:
                        raise ValueError("finding_set_mismatch")
                    for item in actual:
                        original = next(
                            finding
                            for finding in document["findings"]
                            if finding["id"] == item["id"]
                        )
                        if any(
                            item[key] != document[key] for key in ("root", "path", "source_sha256")
                        ):
                            raise ValueError("document_binding_mismatch")
                        _verify_quote(source, item["range"], item["excerpt"])
                        if any(item[key] != original[key] for key in ("range", "excerpt")):
                            raise ValueError("finding_binding_mismatch")
                        for context in item["context"]:
                            _verify_quote(source, context["range"], context["quote"])
                        for suggestion in item["suggestions"]:
                            edit = suggestion["edit"]
                            if edit is not None:
                                _verify_quote(source, edit["range"], edit["original"])
                        for evidence in item["evidence"]:
                            _verify_quote(source, evidence["range"], evidence["source_excerpt"])
                            for suggestion in evidence["suggestions"]:
                                edit = suggestion["edit"]
                                if edit is not None and edit["original"] is not None:
                                    _verify_quote(source, edit["range"], edit["original"])
                    state = "current"
            except (OSError, ValueError, LookupError) as exc:
                # Не раскрываем пути из ошибок файловой системы.
                reason = str(exc) if type(exc) is ValueError else "source_unavailable"
                if reason == "invalid_coordinate_structure":
                    raise InputError("Некорректный или недоступный review-отчёт.") from exc
                if reason in {"quote_mismatch", "coordinate_mismatch", "invalid_byte_range"}:
                    state = "stale"
            results.append({"document_id": identifier, "status": state, "reason": reason})
        status = (
            "stale"
            if any(item["status"] == "stale" for item in results)
            else "unverified"
            if not results or any(item["status"] != "current" for item in results)
            else "current"
        )
        return {
            "schema": "language-review.review-verification",
            "schema_version": "1.0",
            "status": status,
            "documents": results,
        }
    except (OSError, ValueError, LookupError, TypeError) as exc:
        raise InputError("Некорректный или недоступный review-отчёт.") from exc
