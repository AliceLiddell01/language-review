"""Адаптер действительных check/review 1.0; диапазоны сверяются с байтами корпуса."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from language_review.errors import InputError
from language_review.evaluation.contracts import digest, read_json, validate
from language_review.evaluation.corpus import source_map, span
from language_review.extract_report import COORDINATE_SYSTEM
from language_review.review_verify import _validate_links, _validator, _verify_quote, load_review


def load_product_report(path: Path) -> dict[str, Any]:
    report = read_json(path)
    if report.get("schema") == "language-review.review":
        return load_review(path)
    validate(report, "check")
    return report


def analysis_of(report: dict[str, Any]) -> dict[str, Any]:
    return report["analysis"] if report["schema"] == "language-review.review" else report


def _unique(values: list[dict[str, Any]]) -> None:
    if len({value["id"] for value in values}) != len(values):
        raise InputError("Повторный ID в отчёте продукта.")


def bind_documents(
    corpus: dict[str, Any], report: dict[str, Any], bindings: dict[str, str]
) -> dict[str, dict[str, Any]]:
    analysis = analysis_of(report)
    if report["schema"] == "language-review.review":
        try:
            _validator().validate(report)
            _validate_links(report)
        except (ValidationError, ValueError, KeyError) as exc:
            raise InputError("Нарушен контракт review.") from exc
    validate(analysis, "check")
    if analysis["coordinate_system"] != COORDINATE_SYSTEM:
        raise InputError("Неизвестная система координат.")
    _unique(analysis["roots"])
    _unique(analysis["documents"])
    roots = {root["id"] for root in analysis["roots"]}
    repositories = {doc["repository"] for doc in corpus["documents"]}
    if set(bindings) != roots or not set(bindings.values()) <= repositories:
        raise InputError("Требуется явное соответствие всех root ID репозиториям корпуса.")
    lookup = {(doc["repository"], doc["path"]): doc for doc in corpus["documents"]}
    matched: dict[str, dict[str, Any]] = {}
    for doc in analysis["documents"]:
        if doc["root"] not in roots:
            raise InputError("Неизвестный root документа отчёта.")
        target = lookup.get((bindings[doc["root"]], doc["path"]))
        if target is None:
            continue
        if target["id"] in matched:
            raise InputError("Два документа отчёта соответствуют одному исходнику.")
        matched[target["id"]] = doc
        # Отказ чтения исходника остаётся неполнотой, а не доказанным clean.
        if doc["source_sha256"] is None and not doc["fragments"] and not doc["findings"]:
            continue
        if any(
            doc[key] != target[other]
            for key, other in (
                ("source_sha256", "sha256"),
                ("encoding", "encoding"),
                ("bom", "bom"),
            )
        ):
            raise InputError("Происхождение/байты отчёта не соответствуют корпусу.")
        source = source_map(target)
        _unique(doc["fragments"])
        _unique(doc["findings"])
        fragments = {fragment["id"]: fragment for fragment in doc["fragments"]}
        for fragment in fragments.values():
            span(source, fragment["range"])
            if not set(fragment["unit_fragment_ids"]) <= fragments.keys():
                raise InputError("Неизвестный фрагмент единицы проверки.")
        for finding in doc["findings"]:
            span(source, finding["range"])
            if finding["excerpt"] is not None:
                try:
                    _verify_quote(source, finding["range"], finding["excerpt"])
                except ValueError as exc:
                    raise InputError("Цитата не соответствует исходным байтам.") from exc
            if not finding["fragment_ids"] or not set(finding["fragment_ids"]) <= fragments.keys():
                raise InputError("Неизвестный фрагмент находки.")
            for origin in finding["sources"]:
                span(source, origin["range"])
            for suggestion in finding["suggestions"]:
                edit = suggestion["edit"]
                if edit is not None:
                    span(source, edit["range"])
                    if edit["original"] is not None:
                        try:
                            _verify_quote(source, edit["range"], edit["original"])
                        except ValueError as exc:
                            raise InputError("Цитата правки не соответствует исходнику.") from exc
        if report["schema"] == "language-review.review":
            items = {
                item["id"]: item for item in report["items"] if item["document_id"] == doc["id"]
            }
            for finding in doc["findings"]:
                item = items[finding["id"]]
                if any(item[key] != value for key, value in finding.items()) or any(
                    item[key] != doc[key] for key in ("root", "path", "source_sha256")
                ):
                    raise InputError("Несогласованные представления review.")
                for evidence, origin in zip(item["evidence"], item["sources"], strict=True):
                    if any(evidence[key] != value for key, value in origin.items()):
                        raise InputError("Несогласованные свидетельства review.")
                    span(source, evidence["range"])
                    if evidence["source_excerpt"] is not None:
                        try:
                            _verify_quote(source, evidence["range"], evidence["source_excerpt"])
                        except ValueError as exc:
                            raise InputError("Недостоверная цитата evidence.") from exc
                    for suggestion in evidence["suggestions"]:
                        if suggestion["edit"] is not None:
                            span(source, suggestion["edit"]["range"])
                for context in item["context"]:
                    try:
                        _verify_quote(source, context["range"], context["quote"])
                    except ValueError as exc:
                        raise InputError("Недостоверный контекст review.") from exc
    return matched


def candidates(doc: dict[str, Any], engine: str | None = None) -> list[dict[str, Any]]:
    """Финальные элементы и локализованные источники считаются раздельно.

    Check 1.0 не сохраняет индивидуальные исходные replacements после согласования:
    engine-edits ниже доступны только с доказанным происхождением suggestion.engines.
    """
    result = []
    for finding in doc["findings"]:
        origins = finding["sources"] if engine else [finding]
        for origin in origins:
            if engine and origin["engine"] != engine:
                continue
            edits = [
                {"range": item["edit"]["range"], "text": item["edit"]["text"]}
                for item in finding["suggestions"]
                if item["status"] == "candidate"
                and item["edit"] is not None
                and (engine is None or engine in item["engines"])
            ]
            edits = sorted({digest(edit): edit for edit in edits}.values(), key=digest)
            identity = {"range": origin["range"], "category": origin["category"], "edits": edits}
            result.append(
                {
                    **identity,
                    "key": digest(identity),
                    "engines": (
                        [engine]
                        if engine
                        else sorted({item["engine"] for item in finding["sources"]})
                    ),
                    "risks": sorted(
                        {reason for item in finding["suggestions"] for reason in item["reasons"]}
                    ),
                }
            )
    result.sort(key=lambda item: (item["key"], item["engines"]))
    # Мультимножество: повтор не создаёт TP, но остаётся лишним замечанием.
    counts: dict[str, int] = {}
    for item in result:
        key = item["key"]
        counts[key] = counts.get(key, 0) + 1
        item["key"] = f"{key}:{counts[key]}"
    return result
