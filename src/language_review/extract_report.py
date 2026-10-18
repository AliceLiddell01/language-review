from __future__ import annotations

import hashlib
import json
from typing import Any

from language_review import __version__
from language_review.extract_models import ExtractDocument, ExtractResult, FragmentDraft


def _fragment_id(document_id: str, fragment: FragmentDraft) -> str:
    payload = "\0".join(
        (
            document_id,
            fragment.kind,
            fragment.context,
            str(fragment.start_char),
            str(fragment.end_char),
            hashlib.sha256(fragment.text.encode("utf-8")).hexdigest(),
        )
    ).encode("utf-8")
    return "frag-" + hashlib.sha256(payload).hexdigest()[:24]


def _document_mapping(document: ExtractDocument) -> dict[str, Any]:
    source = document.source_map
    fragments: list[dict[str, Any]] = []
    for fragment in document.fragments:
        if source is None:
            continue
        source_text = source.source_text(fragment.start_char, fragment.end_char)
        if fragment.text != source_text:
            raise ValueError("Текст фрагмента не совпадает с точной цитатой исходника.")
        fragments.append(
            {
                "id": _fragment_id(document.id, fragment),
                "kind": fragment.kind,
                "context": fragment.context,
                "text": fragment.text,
                "source_text": source_text,
                "status": fragment.status,
                "reason": fragment.reason,
                "range": source.range_dict(fragment.start_char, fragment.end_char),
                "protected_ranges": [
                    {
                        "reason": span.reason,
                        "range": source.range_dict(span.start_char, span.end_char),
                    }
                    for span in fragment.protected
                ],
            }
        )
    return {
        "id": document.id,
        "root": document.root,
        "path": document.path,
        "language": document.language,
        "format": document.format,
        "profile": document.profile,
        "status": document.status,
        "reason": document.reason,
        "source_sha256": document.source_sha256,
        "encoding": document.encoding,
        "bom": document.bom,
        "diagnostics": sorted(set(document.diagnostics)),
        "fragments": fragments,
    }


def extract_document(result: ExtractResult) -> dict[str, Any]:
    scan_result = result.scan_result
    scan_input_errors = bool(getattr(scan_result, "input_errors", False))
    if scan_input_errors:
        status = "input_error"
    elif any(document.status in {"error", "partial"} for document in result.documents):
        status = "partial"
    elif result.unsupported_count:
        status = "unsupported"
    else:
        status = "ok"
    return {
        "schema": "language-review.extract",
        "schema_version": "1.0",
        "operation": "extract",
        "tool": {"name": "language-review", "version": __version__},
        "status": status,
        "coordinate_system": {
            "byte_offset": "zero-based offset in original file bytes, including BOM",
            "line": "one-based physical line number",
            "column": "one-based Unicode code point column; a tab counts as one code point",
            "utf16_column": "one-based UTF-16 code unit column",
            "end": "exclusive",
        },
        "roots": result.roots,
        "summary": {
            "roots": len(result.roots),
            "documents": len(result.documents),
            "extracted": result.extracted_count,
            "skipped": result.skipped_count,
            "unsupported": result.unsupported_count,
            "partial": sum(document.status == "partial" for document in result.documents),
            "errors": sum(document.status == "error" for document in result.documents),
            "fragments": sum(len(document.fragments) for document in result.documents),
        },
        "documents": [_document_mapping(document) for document in result.documents],
    }


def extract_json(result: ExtractResult) -> str:
    return json.dumps(extract_document(result), ensure_ascii=False, indent=2, sort_keys=True)


def _document_label(document: ExtractDocument) -> str:
    label = f"{document.root} :: {document.path} [{document.status}]"
    if document.language:
        label += f" ({document.language})"
    if document.profile:
        label += f"; профиль {document.profile}"
    if document.reason:
        label += f"; причина {document.reason}"
    return label


def extract_human(result: ExtractResult) -> str:
    lines = ["Структурное извлечение текста"]
    lines.extend(_document_label(document) for document in result.documents)
    for document in result.documents:
        source = document.source_map
        if source is None:
            continue
        for fragment in document.fragments:
            position = source.position(fragment.start_char)
            lines.append(
                f"  {position.line}:{position.column} {fragment.kind} ({fragment.context}): "
                f"{fragment.text}"
            )
    lines.append(
        "Итог: извлечено документов {}, фрагментов {}; пропущено {}; неподдерживаемых {}; "
        "частичных {}; ошибок {}.".format(
            result.extracted_count,
            sum(len(document.fragments) for document in result.documents),
            result.skipped_count,
            result.unsupported_count,
            sum(document.status == "partial" for document in result.documents),
            sum(document.status == "error" for document in result.documents),
        )
    )
    return "\n".join(lines)
