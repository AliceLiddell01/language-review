"""Точное согласие с опубликованными парами, отдельно от метрик независимого эталона."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.evaluation import source_import
from language_review.evaluation.contracts import (
    MAX_BYTES,
    digest,
    jsonl_lines,
    read_json,
    validate,
)
from language_review.evaluation.corpus import (
    contains,
    load_corpus,
    source_map,
    span,
    validate_corpus,
)
from language_review.evaluation.reports import analysis_of, bind_documents, candidates
from language_review.evaluation.scoring import _coverage
from language_review.evaluation.source_import import _validate_recipe, pair_proposal
from language_review.review_verify import _invalid_constant, _unique_objects

PROTOCOL = (
    "published-pair-exact/2; complete-coverage; unique-candidate-per-finding; nonoverlapping-edits"
)


def verified_references(bundle: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Связать внешние исправления с неизменяемыми байтами источника.

    Старые пакеты читаются, включая сгенерированное Markdown-представление.
    Выведенные из пары область и категория не становятся эталоном.
    """
    provenance = read_json(bundle / "provenance.json")
    recipe = source_import.recipes().get(provenance["source"]["id"])
    if recipe is None or provenance["source"] != recipe or recipe["kind"] != "pairs":
        raise InputError("Оценка внешних пар требует зафиксированный источник из каталога.")
    _validate_recipe(recipe)
    files = {}
    for item in recipe["files"]:
        with (bundle / "upstream" / item["path"]).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise InputError("Изменились pinned байты или основание прав reference.")
        files[item["path"]] = raw
    rows = [
        json.loads(line, object_pairs_hook=_unique_objects, parse_constant=_invalid_constant)
        for line in jsonl_lines(files[source_import.SAGE_PAIRS_PATH].decode("utf-8-sig"))
    ]
    references = read_json(bundle / "references.json")
    corpus = load_corpus(bundle / "corpus.json")
    if digest(corpus) != provenance["corpus_sha256"]:
        raise InputError("Корпус source bundle не соответствует provenance.")
    documents = {doc["id"]: doc for doc in corpus["documents"]}
    verified = {}
    for case in corpus["cases"]:
        entry = references.get(case["id"])
        if not isinstance(entry, dict) or type(entry.get("row")) is not int:
            raise InputError("Отсутствует номер исходной reference пары.")
        number = entry["row"]
        if not 1 <= number <= len(rows):
            raise InputError("Reference row вне pinned источника.")
        row = rows[number - 1]
        document = documents[case["document"]]
        if (
            set(row) != {"source", "correction", "domain"}
            or row["domain"] != "GitHubTypoCorpusRu"
            or not all(isinstance(value, str) for value in row.values())
            or source_map(document).raw != row["source"].encode("utf-8")
            or entry["correction"] != row["correction"]
            or document["revision"] != recipe["revision"]
            or document["repository"] != recipe["id"]
            or document["path"] not in {f"records/{number:04d}.md", f"records/{number:04d}.txt"}
            or document["origin"]
            != (
                f"{source_import._url(recipe, source_import.SAGE_PAIRS_PATH)}#row={number}; "
                f"{source_import.SAGE_ORIGIN_NOTE}"
            )
        ):
            raise InputError("Reference не соответствует исходной паре/документу.")
        verified[case["id"]] = {"document": document, "case": case, "row": number, **row}
    return verified, provenance


def change_shape(text: str, correction: str) -> dict[str, Any]:
    """Эвристические формы строковых изменений, без подтверждённых категорий и областей."""
    source = SourceMap.create(text.encode("utf-8"), "utf-8", None)
    proposal = pair_proposal(source, correction)
    if max(len(text), len(correction)) > 20000:
        return {"proposal_status": proposal["status"], "shapes": ["size_limit"], "changes": None}
    changes = [
        op
        for op in SequenceMatcher(None, text, correction, autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    shapes: set[str] = set()
    for op, a, b, c, d in changes:
        old, new = text[a:b], correction[c:d]
        shapes.add(op)
        kinds = {
            "letter"
            if char.isalpha()
            else "punctuation"
            if unicodedata.category(char).startswith("P")
            else "space"
            if char.isspace()
            else "other"
            for char in old + new
        }
        shapes.add(
            "punctuation_only"
            if kinds <= {"punctuation", "space"}
            else "letter_only"
            if kinds == {"letter"}
            else "mixed"
        )
    if re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\d+", text) != re.findall(
        r"[A-Za-z_][A-Za-z_0-9]*|\d+", correction
    ):
        shapes.add("technical_token_changed")
    if len(changes) > 1:
        shapes.add("multiple_changes")
    if not changes:
        shapes.add("unchanged_not_attested_clean")
    return {
        "proposal_status": proposal["status"],
        "shapes": sorted(shapes),
        "changes": len(changes),
    }


def _reconstruct(
    document: dict[str, Any], doc: dict[str, Any], engine: str | None
) -> tuple[str | None, str | None, int]:
    source = source_map(document)
    edits: dict[tuple[int, int, str], None] = {}
    for finding in candidates(doc, engine):
        choices = finding["edits"]
        if len(choices) > 1:
            return None, "multiple_candidate_alternatives", 0
        if not choices:
            continue
        edit = choices[0]
        start, end = span(source, edit["range"])
        edits[(start, end, edit["text"])] = None
    ordered = sorted(edits)
    for first, second in zip(ordered, ordered[1:], strict=False):
        if first[1] > second[0] or first[0] == second[0]:
            return None, "conflicting_candidate_edits", 0
    raw = source.raw
    for start, end, replacement in reversed(ordered):
        raw = raw[:start] + replacement.encode(source.encoding) + raw[end:]
    return raw.decode(source.encoding), None, len(ordered)


def reference_score(
    corpus: dict[str, Any],
    report: dict[str, Any],
    bundle: Path,
    split: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    validate_corpus(corpus)
    validate(manifest, "run")
    if any(
        manifest[key] != value
        for key, value in (
            ("corpus_sha256", digest(corpus)),
            ("report_sha256", digest(report)),
            ("split", split),
        )
    ):
        raise InputError("Манифест reference assessment не соответствует входам.")
    references, provenance = verified_references(bundle)
    bound = bind_documents(corpus, report, manifest["root_bindings"])
    analysis = analysis_of(report)
    documents = {doc["id"]: doc for doc in corpus["documents"]}
    scopes: dict[str, Any] = {}
    for engine in [None, *sorted(item["name"] for item in analysis["engines"])]:
        rows = []
        for case in corpus["cases"]:
            if case["split"] != split:
                continue
            document = documents[case["document"]]
            source = source_map(document)
            ref = references.get(case["id"])
            doc = bound.get(case["document"])
            coverage, reasons = _coverage(case, document, doc, engine)
            state = "undetermined"
            reason: str | None = "incomplete_coverage"
            exact, edited, shape = None, None, None
            applicable = False
            if ref is None:
                state, reason = "rejected", "reference_missing"
            elif (
                ref["document"] != document
                or not contains(case["range"], source.range_dict(0, len(source.text)))
                or any(
                    case[key] != ref["case"][key]
                    for key in ("range", "protected_ranges", "categories", "profile")
                )
            ):
                state, reason = "rejected", "reference_document_or_full_pair_mismatch"
            else:
                shape = change_shape(source.text, ref["correction"])
                applicable = True
                if "size_limit" in shape["shapes"]:
                    state, reason = "rejected", "pair_size_limit"
                    applicable = False
                elif coverage["evaluation_ready"] and doc is not None:
                    prediction, reason, edited = _reconstruct(document, doc, engine)
                    if reason is None:
                        state, exact = "evaluated", prediction == ref["correction"]
            rows.append(
                {
                    "id": case["id"],
                    "row": ref["row"] if ref else None,
                    "state": state,
                    "reason": reason,
                    "applicable": applicable,
                    "exact": exact,
                    "candidate_edits": edited,
                    "change_shape": shape,
                    "coverage": coverage,
                    "coverage_reasons": reasons,
                }
            )
        counts = Counter(row["state"] for row in rows)
        evaluated = counts["evaluated"]
        matches = sum(row["exact"] is True for row in rows)
        summary = {
            "pairs": len(rows),
            "applicable": sum(row["applicable"] for row in rows),
            **{key: counts[key] for key in ("evaluated", "undetermined", "rejected")},
            "exact_matches": matches,
            "denominator": evaluated,
            "exact_match_rate": matches / evaluated if evaluated else None,
            "reasons": dict(Counter(row["reason"] for row in rows if row["reason"])),
            "heuristic_shapes": dict(
                Counter(
                    s for row in rows if row["change_shape"] for s in row["change_shape"]["shapes"]
                )
            ),
            "human_category_counts": {},
            "status": "NOT_RUN"
            if not evaluated
            else "PARTIAL"
            if evaluated < len(rows)
            else "PASS",
        }
        scopes[engine or "pipeline"] = {"summary": summary, "pairs": rows}
    result = {
        "schema": "language-review.reference-assessment",
        "schema_version": "1.0",
        "report_kind": "external_reference_assessment",
        "protocol": PROTOCOL,
        "inputs": {
            "corpus_sha256": digest(corpus),
            "report_sha256": digest(report),
            "references_sha256": digest(read_json(bundle / "references.json")),
            "provenance_sha256": digest(provenance),
            "manifest_sha256": digest(manifest),
            "split": split,
        },
        "source": provenance["source"],
        "scopes": scopes,
        "limitations": [
            "Только совпадение с опубликованной строкой исправления; без detection TP/FP/FN.",
            "Нет исчерпывающих категорий эталона, подтверждения чистоты и оценки смыслового вреда.",
            "Внешний публичный test; присутствие в обучающих данных не исключено.",
            "Формы строкового diff — эвристики, а не человеческие категории ошибок.",
            "Реконструкция в памяти; без записи исходников и выбора варианта по эталону.",
            "Отклонённые выходы не FP; находка без правки сохраняет исходную строку.",
        ],
    }
    validate(result, "reference")
    return result
