"""Повторная сверка сохранённого ревью и точных координат только для чтения."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from language_review.coordinates import SourceMap
from language_review.errors import InputError

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


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = resources.files("language_review").joinpath("review-report.schema.json")
    return Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))


def _unique_objects(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_json_key")
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("invalid_json_constant")


def _identities(values: list[dict[str, Any]]) -> set[str]:
    identifiers = {value["id"] for value in values}
    if len(identifiers) != len(values):
        raise ValueError("duplicate_identity")
    return identifiers


def _validate_ranges(value: Any) -> None:
    if isinstance(value, dict):
        if set(value) == {"start", "end"} and isinstance(value["start"], dict):
            first, last = value["start"], value["end"]
            if first["byte_offset"] > last["byte_offset"]:
                raise ValueError("invalid_range_order")
        for key, child in value.items():
            if key != "details":
                _validate_ranges(child)
    elif isinstance(value, list):
        for child in value:
            _validate_ranges(child)


def _validate_links(report: dict[str, Any]) -> None:
    analysis = report["analysis"]
    roots = _identities(analysis["roots"])
    _identities(analysis["documents"])
    _identities(report["items"])
    documents = {value["id"]: value for value in analysis["documents"]}
    findings: dict[str, dict[str, Any]] = {}
    fragments: dict[str, set[str]] = {}
    items_by_document: dict[str, list[dict[str, Any]]] = {key: [] for key in documents}
    for document in documents.values():
        if document["root"] not in roots:
            raise ValueError("unknown_root")
        fragments[document["id"]] = _identities(document["fragments"])
        _identities(document["findings"])
        for finding in document["findings"]:
            if finding["id"] in findings:
                raise ValueError("duplicate_identity")
            findings[finding["id"]] = document
    if set(findings) != {item["id"] for item in report["items"]}:
        raise ValueError("finding_set_mismatch")
    for item in report["items"]:
        if (
            item["document_id"] not in documents
            or findings[item["id"]]["id"] != item["document_id"]
        ):
            raise ValueError("unknown_document")
        items_by_document[item["document_id"]].append(item)
        if any(
            related == item["id"] or findings.get(related) is not documents[item["document_id"]]
            for related in item["related_findings"]
        ):
            raise ValueError("unknown_related_finding")
    for document in documents.values():
        known = fragments[document["id"]]
        values = [*document["findings"], *document["rejected"]]
        values += items_by_document[document["id"]]
        for value in values:
            if not set(value["fragment_ids"]) <= known:
                raise ValueError("unknown_fragment")
            for context in value.get("context", []):
                if context["fragment_id"] not in known:
                    raise ValueError("unknown_fragment")
        for fragment in document["fragments"]:
            if not set(fragment["unit_fragment_ids"]) <= known:
                raise ValueError("unknown_fragment")
    for value in [*report["filtered"], *report["unlocalized"]]:
        if (
            value["document_id"] not in documents
            or not set(value["fragment_ids"]) <= fragments[value["document_id"]]
        ):
            raise ValueError("unknown_fragment")


def load_review(report_path: Path) -> dict[str, Any]:
    try:
        report: dict[str, Any] = json.loads(
            _read(report_path), object_pairs_hook=_unique_objects, parse_constant=_invalid_constant
        )
        _validator().validate(report)
        _validate_ranges(report)
        _validate_links(report)
        return report
    except (OSError, ValueError, LookupError, TypeError, RecursionError, ValidationError) as exc:
        raise InputError("Некорректный или недоступный review-отчёт.") from exc


def verify_review(report_path: Path, roots: dict[str, Path]) -> dict[str, Any]:
    try:
        report = load_review(report_path)
        analysis = report["analysis"]
        root_kinds = {root["id"]: root["kind"] for root in analysis["roots"]}
        documents, items = analysis["documents"], report["items"]
        items_by_document: dict[str, list[dict[str, Any]]] = {
            document["id"]: [] for document in documents
        }
        for item in items:
            items_by_document[item["document_id"]].append(item)
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
                    findings = {finding["id"]: finding for finding in document["findings"]}
                    for item in items_by_document[identifier]:
                        original = findings[item["id"]]
                        if any(
                            item[key] != document[key] for key in ("root", "path", "source_sha256")
                        ):
                            raise ValueError("document_binding_mismatch")
                        _verify_quote(source, item["range"], item["excerpt"])
                        _verify_quote(source, original["range"], original["excerpt"])
                        for entry in (item, original):
                            for suggestion in entry["suggestions"]:
                                edit = suggestion["edit"]
                                if edit is not None:
                                    _verify_quote(source, edit["range"], edit["original"])
                            for origin in entry["sources"]:
                                location = origin["range"]
                                start = location["start"]["byte_offset"]
                                end = location["end"]["byte_offset"]
                                quote = source.raw[start:end].decode(source.encoding)
                                _verify_quote(source, location, quote)
                        if any(item[key] != value for key, value in original.items()):
                            raise InputError("Несогласованные представления review-отчёта.")
                        for context in item["context"]:
                            _verify_quote(source, context["range"], context["quote"])
                        if len(item["evidence"]) != len(item["sources"]):
                            raise InputError("Несогласованные свидетельства review-отчёта.")
                        for evidence, origin in zip(item["evidence"], item["sources"], strict=True):
                            _verify_quote(source, evidence["range"], evidence["source_excerpt"])
                            if any(evidence[key] != value for key, value in origin.items()):
                                raise InputError("Несогласованные свидетельства review-отчёта.")
                            for suggestion in evidence["suggestions"]:
                                edit = suggestion["edit"]
                                if edit is not None and edit["original"] is not None:
                                    _verify_quote(source, edit["range"], edit["original"])
                                if not any(
                                    value["edit"] == edit
                                    and value["status"] == suggestion["status"]
                                    and evidence["engine"] in value["engines"]
                                    for value in item["suggestions"]
                                ):
                                    raise InputError("Несогласованные варианты review-отчёта.")
                    state = "current"
            except LookupError as exc:
                raise InputError("Некорректный или недоступный review-отчёт.") from exc
            except UnicodeError as exc:
                raise InputError("Некорректный или недоступный review-отчёт.") from exc
            except OSError:
                # Не раскрываем пути из ошибок файловой системы.
                reason = "source_unavailable"
            except ValueError as exc:
                reason = str(exc)
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
