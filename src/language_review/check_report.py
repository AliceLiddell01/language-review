from __future__ import annotations

from typing import Any

from language_review import __version__
from language_review.check import fragments_in_range, source_excerpt, source_position
from language_review.check_models import (
    CheckDocument,
    CheckResult,
    CheckUnit,
    EngineRun,
    Finding,
    RejectedOutput,
)
from language_review.extract_report import COORDINATE_SYSTEM

SCHEMA = "language-review.check"
SCHEMA_VERSION = "1.0"
EXIT_CODES = {"clean": 0, "findings": 1, "error": 2, "partial": 4, "unavailable": 5}
STATUS_LABELS = {
    "clean": "замечаний нет",
    "findings": "есть замечания",
    "partial": "проверка неполная",
    "unavailable": "движок недоступен или завершился ошибкой",
    "error": "ошибка входных данных",
}


def check_exit_code(result: CheckResult) -> int:
    return EXIT_CODES[result.status]


def _range(unit: CheckUnit, start: int, end: int) -> dict[str, dict[str, int]]:
    return {"start": source_position(unit, start), "end": source_position(unit, end)}


def _engine_mapping(run: EngineRun) -> dict[str, Any]:
    return {
        "name": run.name,
        "status": run.status,
        "details": dict(sorted(run.details.items())),
        "diagnostics": list(run.diagnostics),
        "message": run.message,
        "statistics": {
            "units_total": run.units_total,
            "units_checked": run.units_checked,
            "units_failed": run.units_failed,
            "units_skipped": run.units_skipped,
            "fragments_checked": run.fragments_checked,
            "findings": run.findings,
            "rejected": run.rejected,
            "filtered": run.filtered,
            "requests": run.requests,
            "request_failures": run.request_failures,
        },
        "elapsed_ms": run.elapsed_ms,
        "tokens": dict(sorted(run.tokens.items())),
    }


def _finding_mapping(finding: Finding) -> dict[str, Any]:
    unit = finding.candidates[0].unit
    excerpt = source_excerpt(unit, finding.source_start, finding.source_end)
    sources = []
    for candidate in finding.candidates:
        raw = candidate.raw
        sources.append(
            {
                "engine": raw.engine,
                "rule_id": raw.rule_id,
                "rule_category": raw.rule_category,
                "category": raw.category,
                "message": raw.message,
                "confidence": raw.confidence,
                "severity": raw.severity,
                "engine_excerpt": raw.engine_excerpt,
                "range": _range(unit, candidate.source_start, candidate.source_end),
            }
        )
    suggestions = []
    for suggestion in finding.suggestions:
        edit = None
        if suggestion.edit is not None:
            start, end, text = suggestion.edit
            edit = {
                "range": _range(unit, start, end),
                "original": source_excerpt(unit, start, end),
                "text": text,
            }
        suggestions.append(
            {
                "status": suggestion.status,
                "reasons": list(suggestion.reasons),
                "origin": suggestion.origin,
                "engines": list(suggestion.engines),
                "engine_replacement": suggestion.engine_replacement,
                "edit": edit,
            }
        )
    candidates = {
        (item.edit, item.engine_replacement)
        for item in finding.suggestions
        if item.status == "candidate"
    }
    return {
        "id": finding.id,
        "category": finding.category,
        "categories": sorted({item.raw.category for item in finding.candidates}),
        "fragment_ids": fragments_in_range(unit, finding.source_start, finding.source_end),
        "range": _range(unit, finding.source_start, finding.source_end),
        "excerpt": excerpt,
        "excerpt_status": "verified" if excerpt is not None else "spans_fragments",
        "agreement": sorted({item.raw.engine for item in finding.candidates}),
        "conflicting_suggestions": len({edit for edit, _ in candidates}) > 1,
        "sources": sources,
        "suggestions": suggestions,
    }


def _rejected_mapping(document: CheckDocument, rejected: RejectedOutput) -> dict[str, Any]:
    unit = next(item for item in document.units if item.index == rejected.unit)
    return {
        "engine": rejected.engine,
        "reason": rejected.reason,
        "fragment_ids": list(unit.fragment_ids),
        "rule_id": rejected.rule_id,
        "message": rejected.message,
        "excerpt": rejected.excerpt,
    }


def _fragment_checks(document: CheckDocument) -> list[dict[str, Any]]:
    fragments = []
    for unit in document.units:
        checks = {
            engine: {
                "status": document.unit_status.get((unit.index, engine), ("failed", None))[0],
                "reason": document.unit_status.get((unit.index, engine), ("failed", None))[1],
            }
            for engine in document.engines
        }
        for identifier, fragment in zip(unit.fragment_ids, unit.fragments, strict=True):
            fragments.append(
                {
                    "id": identifier,
                    "kind": fragment.draft.kind,
                    "context": fragment.draft.context,
                    "range": fragment.range,
                    "unit_fragment_ids": list(unit.fragment_ids),
                    "checks": checks,
                }
            )
    return fragments


def _document_mapping(document: CheckDocument) -> dict[str, Any]:
    extract = document.extract
    rejected = sorted(
        (_rejected_mapping(document, item) for item in document.rejected),
        key=lambda item: (
            item["fragment_ids"],
            item["engine"],
            item["reason"],
            item["rule_id"] or "",
            item["excerpt"] or "",
        ),
    )
    return {
        "id": extract.id,
        "root": extract.root,
        "path": extract.path,
        "language": extract.language,
        "format": extract.format,
        "profile": extract.profile,
        "status": document.status,
        "extraction_status": extract.status,
        "extraction_reason": extract.reason,
        "source_sha256": extract.source_sha256,
        "encoding": extract.encoding,
        "bom": extract.bom,
        "engines": list(document.engines),
        "diagnostics": list(document.diagnostics),
        "fragments": _fragment_checks(document),
        "findings": [_finding_mapping(finding) for finding in document.findings],
        "rejected": rejected,
    }


def check_document(result: CheckResult) -> dict[str, Any]:
    documents = result.documents
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "operation": "check",
        "tool": {"name": "language-review", "version": __version__},
        "status": result.status,
        "suggestions_applied": False,
        "coordinate_system": COORDINATE_SYSTEM,
        "configuration": {
            "engines": result.settings["engines"],
            "categories": result.settings["categories"],
            "llm_provider": result.settings["llm_provider"],
            "sources": result.settings["sources"],
        },
        "engines": [_engine_mapping(run) for run in result.engines],
        "roots": result.extract.roots,
        "summary": {
            "roots": len(result.extract.roots),
            "documents": len(documents),
            "clean": sum(item.status == "clean" for item in documents),
            "with_findings": sum(item.status == "findings" for item in documents),
            "partial": sum(item.status == "partial" for item in documents),
            "unavailable": sum(item.status == "unavailable" for item in documents),
            "errors": sum(item.status == "error" for item in documents),
            "skipped": sum(item.status == "skipped" for item in documents),
            "unsupported": sum(item.status == "unsupported" for item in documents),
            "fragments": sum(len(item.extract.fragments) for item in documents),
            "units": sum(len(item.units) for item in documents),
            "units_without_russian_text": sum(
                unit.skip_reason == "no_russian_text" for item in documents for unit in item.units
            ),
            "findings": sum(len(item.findings) for item in documents),
            "rejected": sum(len(item.rejected) for item in documents),
        },
        "documents": [_document_mapping(document) for document in documents],
    }


def _engine_line(run: EngineRun) -> str:
    details = run.details
    label = run.name
    if run.name == "llm":
        label += f" ({details.get('provider')}, модель {details.get('model') or 'не выбрана'})"
    version = details.get("version") or details.get("server_build") or details.get("server_version")
    if version:
        label += f", версия {version}"
    line = (
        f"  {label}: {run.status}; проверено единиц {run.units_checked}/{run.units_total}, "
        f"сбоев {run.units_failed}, пропусков {run.units_skipped}, находок {run.findings}, "
        f"отклонено {run.rejected}, {run.elapsed_ms:.0f} мс"
    )
    if run.message:
        line += f" — {run.message}"
    if run.diagnostics:
        line += f" [{', '.join(run.diagnostics)}]"
    return line


def check_human(result: CheckResult) -> str:
    lines = [f"Языковая проверка: {result.status} — {STATUS_LABELS[result.status]}"]
    lines.append("Движки:")
    lines.extend(_engine_line(run) for run in result.engines)
    for document in result.documents:
        extract = document.extract
        header = f"{extract.root} :: {extract.path} [{document.status}]"
        if extract.status not in {"extracted", "skipped"}:
            header += f"; извлечение: {extract.status}"
        if extract.reason:
            header += f"; причина {extract.reason}"
        if document.diagnostics:
            header += f" [{', '.join(document.diagnostics)}]"
        lines.append(header)
        for finding in document.findings:
            mapping = _finding_mapping(finding)
            start = mapping["range"]["start"]
            engines = ", ".join(
                (
                    f"{source['engine']}:{source['rule_id']}"
                    if source["rule_id"]
                    else source["engine"]
                )
                + (f", уверенность модели {source['confidence']}" if source["confidence"] else "")
                for source in mapping["sources"]
            )
            excerpt = mapping["excerpt"]
            quoted = f"«{excerpt}» " if excerpt is not None else ""
            lines.append(
                f"  {start['line']}:{start['column']} [{finding.category}] {quoted}"
                f"— {mapping['sources'][0]['message']} ({engines})"
            )
            for source in mapping["sources"][1:]:
                lines.append(f"      {source['engine']}: {source['message']}")
            candidates = [
                item["edit"]["text"]
                for item in mapping["suggestions"]
                if item["status"] == "candidate" and item["edit"] is not None
            ]
            if candidates:
                lines.append(
                    "      варианты (требуют ручной проверки): "
                    + " | ".join(f"«{item}»" for item in candidates)
                )
            rejected = [item for item in mapping["suggestions"] if item["status"] == "rejected"]
            if rejected:
                reasons = sorted({reason for item in rejected for reason in item["reasons"]})
                lines.append(f"      отклонено предложений: {len(rejected)} ({', '.join(reasons)})")
        if document.rejected:
            reasons = sorted({item.reason for item in document.rejected})
            lines.append(
                f"  отбраковано результатов движков: {len(document.rejected)} "
                f"({', '.join(reasons)})"
            )
    findings = sum(len(item.findings) for item in result.documents)
    lines.append(
        f"Итог: документов {len(result.documents)}, замечаний {findings}; "
        "предложения не применялись к исходникам."
    )
    return "\n".join(lines)
