"""Строгое сопоставление кандидатов и прозрачная арифметика без языкового судьи."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from language_review.errors import InputError
from language_review.evaluation import METRIC_VERSION, SCORER_VERSION
from language_review.evaluation.adjudication import apply_judgments
from language_review.evaluation.contracts import digest, validate
from language_review.evaluation.corpus import (
    contains,
    eligible,
    overlaps,
    source_map,
    span,
    validate_corpus,
)
from language_review.evaluation.reports import analysis_of, bind_documents, candidates


def metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    denominators = {
        "precision": tp + fp,
        "recall": tp + fn,
        "f1": 2 * tp + fp + fn,
        "f0_5": 1.25 * tp + fp + 0.25 * fn,
    }
    numerators = {"precision": tp, "recall": tp, "f1": 2 * tp, "f0_5": 1.25 * tp}
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        **{key: numerators[key] / value if value else None for key, value in denominators.items()},
        "denominators": denominators,
        "undefined": [
            f"{key}: zero denominator" for key, value in denominators.items() if not value
        ],
    }


def _coverage(
    case: dict[str, Any],
    document: dict[str, Any],
    doc: dict[str, Any] | None,
    engine: str | None,
) -> tuple[dict[str, Any], list[str]]:
    coverage: dict[str, Any] = {
        "observed_document": doc is not None,
        "extracted": False,
        "applicable": False,
        "checked": False,
        "fragments": 0,
        "engine_statuses": {},
        "missing_characters": None,
    }
    if doc is None:
        return coverage, ["before:document_missing_scan_or_selection_unknown"]
    reasons = []
    if case["profile"] is not None and case["profile"] != doc["profile"]:
        reasons.append("before:profile_mismatch")
    relevant = [frag for frag in doc["fragments"] if overlaps(case["range"], frag["range"])]
    coverage["fragments"] = len(relevant)
    coverage["extracted"] = bool(relevant)
    source = source_map(document)
    first, last = span(source, case["range"])
    offset = source.position(0).byte_offset
    missing = 0
    for character in source.text:
        end = offset + len(character.encode(source.encoding))
        if first <= offset < last and not character.isspace():
            protected = any(
                value["start"]["byte_offset"] <= offset < value["end"]["byte_offset"]
                for value in case["protected_ranges"]
            )
            if not protected and not any(
                frag["range"]["start"]["byte_offset"] <= offset
                and end <= frag["range"]["end"]["byte_offset"]
                for frag in relevant
            ):
                missing += 1
        offset = end
    coverage["missing_characters"] = missing
    if missing or not relevant:
        reasons.append("before:extract_uncovered")
    if doc["extraction_status"] != "extracted":
        reasons.append(f"before:extraction_{doc['extraction_status']}")
    configured_categories = doc["configuration"]["categories"]
    # Пустой список в действующем check означает отсутствие фильтра, то есть все категории.
    missing_categories = (
        sorted(set(case["categories"]) - set(configured_categories))
        if configured_categories
        else []
    )
    if missing_categories:
        reasons.append("before:category_disabled:" + ",".join(missing_categories))
    selected = [engine] if engine else doc["configuration"]["engines"]
    checked = bool(relevant)
    applicable = False
    for frag in relevant:
        successes = 0
        for name in selected:
            check = frag["checks"].get(name, {"status": "skipped", "reason": "engine_not_selected"})
            coverage["engine_statuses"][name + ":" + check["status"]] = (
                coverage["engine_statuses"].get(name + ":" + check["status"], 0) + 1
            )
            if check["status"] == "checked":
                successes += 1
                applicable = True
            elif check["reason"] != "no_russian_text":
                reasons.append(f"routing_or_engine:{name}:{check['reason'] or check['status']}")
                checked = False
        if not successes:
            checked = False
            reasons.append("before:no_applicable_checked_engine")
    coverage["applicable"] = applicable
    coverage["checked"] = checked
    ids = {frag["id"] for frag in relevant}
    for rejected in doc["rejected"]:
        if ids.intersection(rejected["fragment_ids"]) and (
            engine is None or rejected["engine"] == engine
        ):
            reasons.append("after:rejected:" + rejected["reason"])
    if doc["status"] in {"partial", "unavailable", "error", "skipped", "unsupported"}:
        reasons.append("document:" + doc["status"])
    return coverage, sorted(set(reasons))


def _edited_bytes(document: dict[str, Any], edit: dict[str, Any]) -> bytes:
    source = source_map(document)
    start, end = span(source, edit["range"])
    replacement: bytes = edit["text"].encode(source.encoding)
    return source.raw[:start] + replacement + source.raw[end:]


def score_case(
    case: dict[str, Any],
    document: dict[str, Any],
    doc: dict[str, Any] | None,
    proposals: list[dict[str, Any]],
    engine: str | None,
) -> dict[str, Any]:
    coverage, reasons = _coverage(case, document, doc, engine)
    allowed = eligible(case)
    state = "not_evaluated" if not allowed else "incomplete" if reasons else "evaluated"
    if not allowed:
        reasons.append("annotation:not_verified_exhaustive_applicable")
    gold = case["labels"] if allowed else []
    matched: set[str] = set()
    uncertain: set[str] = set()
    outcomes = []
    diagnosed: set[str] = set()
    for proposal in proposals if allowed else []:
        if proposal["category"] not in case["categories"]:
            outcomes.append(
                {
                    **proposal,
                    "outcome": "not_evaluated",
                    "label": None,
                    "duplicate": False,
                    "correction": "unknown",
                    "harm": None,
                    "decision_provenance": "reference_matching",
                }
            )
            continue
        exact = [
            label
            for label in gold
            if label["category"] == proposal["category"] and proposal["range"] in label["anchors"]
        ]
        near = [
            label
            for label in gold
            if label["category"] == proposal["category"]
            and any(overlaps(proposal["range"], anchor) for anchor in label["anchors"])
        ]
        identity = digest({"range": proposal["range"], "category": proposal["category"]})
        duplicate = identity in diagnosed
        diagnosed.add(identity)
        label = exact[0] if len(exact) == 1 else None
        if len(exact) > 1 or (not exact and near) or not contains(case["range"], proposal["range"]):
            outcome = "needs_adjudication"
            uncertain.update(value["id"] for value in exact or near)
            label = None
        elif label and label["id"] not in matched:
            outcome = "tp"
            matched.add(label["id"])
        else:
            outcome = "fp"
        correction = "absent"
        if proposal["edits"]:
            if outcome == "needs_adjudication":
                correction = "needs_adjudication"
            elif outcome == "fp":
                correction = "incorrect"
            elif label and not label["corrections"]:
                correction = "unknown"
            elif label:
                accepted = {_edited_bytes(document, edit) for edit in label["corrections"]}
                correctness = [
                    _edited_bytes(document, edit) in accepted for edit in proposal["edits"]
                ]
                # Не выбираем удачную первую рекомендацию среди неверных альтернатив.
                correction = (
                    "correct"
                    if all(correctness)
                    else "incorrect"
                    if not any(correctness)
                    else "needs_adjudication"
                )
        risks = set(proposal["risks"])
        if any(
            overlaps(edit["range"], protected)
            for edit in proposal["edits"]
            for protected in case["protected_ranges"]
        ):
            risks.add("technical_protected_range")
        outcomes.append(
            {
                **proposal,
                "outcome": outcome,
                "label": label["id"] if label else None,
                "duplicate": duplicate,
                "correction": correction,
                "risks": sorted(risks),
                "harm": None,
                "decision_provenance": "reference_matching",
            }
        )
    if any(
        item["outcome"] == "needs_adjudication" or item["correction"] == "needs_adjudication"
        for item in outcomes
    ):
        state = "incomplete"
        reasons.append("matching:needs_adjudication")
    return {
        "id": case["id"],
        "document": document["id"],
        "data_kind": document["data_kind"],
        "format": document["path"].rsplit(".", 1)[-1],
        "context": case["context"],
        "categories": case["categories"],
        "label_status": case["annotation"]["status"],
        "state": state,
        "reasons": sorted(set(reasons)),
        "eligible": allowed,
        "gold": [
            {
                "id": label["id"],
                "category": label["category"],
                "correction_known": bool(label["corrections"]),
            }
            for label in gold
        ],
        "tp": sorted(matched),
        "fn": sorted({label["id"] for label in gold} - matched - uncertain),
        "clean": case["verdict"] == "clean",
        "candidates": outcomes,
        "coverage": {**coverage, "unresolved_gold": sorted(uncertain - matched)},
        "engine": engine,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [case for case in cases if case["state"] == "evaluated"]
    allowed = [case for case in cases if case["eligible"]]

    def counts(values: list[dict[str, Any]]) -> tuple[int, int, int]:
        return (
            sum(len(case["tp"]) for case in values),
            sum(item["outcome"] == "fp" for case in values for item in case["candidates"]),
            sum(len(case["fn"]) for case in values),
        )

    correction_tp = correction_fp = correction_fn = unknown = 0
    for case in allowed:
        correct = {item["label"] for item in case["candidates"] if item["correction"] == "correct"}
        correction_tp += len(correct)
        correction_fp += sum(item["correction"] == "incorrect" for item in case["candidates"])
        correction_fn += sum(
            label["correction_known"] and label["id"] not in correct for label in case["gold"]
        )
        unknown += sum(item["correction"] == "unknown" for item in case["candidates"])
    fp = counts(completed)[1]
    return {
        "detection": metrics(*counts(completed)),
        "pipeline_detection": metrics(*counts(allowed)),
        "correction": metrics(correction_tp, correction_fp, correction_fn),
        "cases": len(cases),
        "eligible_cases": len(allowed),
        "evaluated_applicable_cases": len(completed),
        "incomplete_cases": sum(case["state"] == "incomplete" for case in cases),
        "not_evaluated_cases": sum(case["state"] == "not_evaluated" for case in cases),
        "tn_clean_cases": sum(
            case["clean"]
            and not any(item["outcome"] != "not_evaluated" for item in case["candidates"])
            for case in completed
        ),
        "clean_cases_with_fp": sum(
            case["clean"] and any(item["outcome"] == "fp" for item in case["candidates"])
            for case in completed
        ),
        "fp_per_1000_evaluated_applicable_cases": 1000 * fp / len(completed) if completed else None,
        "overediting": sum(
            case["clean"]
            and item["category"] in {"style", "bureaucratese", "terminology"}
            and item["outcome"] == "fp"
            for case in allowed
            for item in case["candidates"]
        ),
        "needs_adjudication": sum(
            item["outcome"] == "needs_adjudication" or item["correction"] == "needs_adjudication"
            for case in allowed
            for item in case["candidates"]
        ),
        "unknown_corrections": unknown,
        "coverage": {
            key: sum(bool(case["coverage"][key]) for case in cases)
            for key in ("observed_document", "extracted", "applicable", "checked")
        },
    }


def _strata(cases: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dimension in ("format", "context", "data_kind"):
        result[dimension] = {
            key: summarize([case for case in cases if case[dimension] == key])
            for key in sorted({case[dimension] for case in cases})
        }
    # Категория фильтрует и gold, и кандидатов; случай с несколькими категориями не удваивает TP.
    result["category"] = {}
    for category in sorted(
        {value for case in cases for value in case["categories"]}
        | {item["category"] for case in cases for item in case["candidates"]}
    ):
        group = []
        for case in cases:
            gold = [label for label in case["gold"] if label["category"] == category]
            proposals = [item for item in case["candidates"] if item["category"] == category]
            if category not in case["categories"] and not gold and not proposals:
                continue
            ids = {label["id"] for label in gold}
            group.append(
                {
                    **case,
                    "gold": gold,
                    "tp": [key for key in case["tp"] if key in ids],
                    "fn": [key for key in case["fn"] if key in ids],
                    "candidates": proposals,
                }
            )
        result["category"][category] = summarize(group)
    return result


def score(
    corpus: dict[str, Any],
    report: dict[str, Any],
    bindings: dict[str, str],
    split: str,
    run_manifest: dict[str, Any] | None = None,
    judgments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_corpus(corpus)
    if run_manifest is not None:
        validate(run_manifest, "run")
        if any(
            run_manifest[key] != value
            for key, value in (
                ("corpus_sha256", digest(corpus)),
                ("report_sha256", digest(report)),
                ("root_bindings", bindings),
                ("split", split),
            )
        ):
            raise InputError("Манифест не соответствует корпусу, отчёту, split или roots.")
    documents = {doc["id"]: doc for doc in corpus["documents"]}
    bound = bind_documents(corpus, report, bindings)
    analysis = analysis_of(report)
    selected = [case for case in corpus["cases"] if case["split"] == split]
    engine_names = sorted({engine["name"] for engine in analysis["engines"]})
    rows: dict[str | None, list[dict[str, Any]]] = {key: [] for key in [None, *engine_names]}
    ignored: Counter[str] = Counter()
    for engine in rows:
        assigned: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for identifier, doc in bound.items():
            for proposal in candidates(doc, engine):
                owner = next(
                    (
                        case
                        for case in selected
                        if case["document"] == identifier
                        and overlaps(case["range"], proposal["range"])
                    ),
                    None,
                )
                if owner is None:
                    ignored[engine or "pipeline"] += 1
                else:
                    assigned[owner["id"]].append(proposal)
        for case in selected:
            rows[engine].append(
                score_case(
                    case,
                    documents[case["document"]],
                    bound.get(case["document"]),
                    assigned[case["id"]],
                    engine,
                )
            )
    if judgments is not None:
        apply_judgments(judgments, corpus, report, rows)
    cases = rows[None]
    kinds = {case["data_kind"] for case in cases}
    kind = (
        "synthetic_contract_check"
        if kinds == {"synthetic"}
        else "independent_baseline"
        if kinds == {"real"} and all(case["eligible"] for case in cases)
        else "exploratory"
    )
    summary = summarize(cases)
    status = (
        "NOT_RUN"
        if not cases or not summary["eligible_cases"] or not summary["coverage"]["checked"]
        else "PARTIAL"
        if summary["incomplete_cases"] or summary["not_evaluated_cases"]
        else "PASS"
    )
    contribution: dict[str, Any] = {}
    final_gold = {(case["id"], key) for case in cases for key in case["tp"]}
    by_engine = {
        engine: {(case["id"], key) for case in rows[engine] for key in case["tp"]}
        for engine in engine_names
    }
    for engine in engine_names:
        peers = set().union(*(value for key, value in by_engine.items() if key != engine))
        contribution[engine] = {
            "unique_final_tp": [
                list(value) for value in sorted((by_engine[engine] & final_gold) - peers)
            ],
            "shared_final_tp": [
                list(value) for value in sorted(by_engine[engine] & final_gold & peers)
            ],
        }
    compatibility = {
        "corpus_id": corpus["id"],
        "corpus_version": corpus["version"],
        "annotation_version": corpus["annotation_version"],
        "corpus_sha256": digest(corpus),
        "split": split,
        "metric_version": METRIC_VERSION,
        "scorer_version": SCORER_VERSION,
        "report_kind": kind,
        "judgment_policy": "independent_judgments" if judgments is not None else "reference_only",
    }
    result = {
        "schema": "language-review.evaluation",
        "schema_version": "1.0",
        "scorer_version": SCORER_VERSION,
        "metric_version": METRIC_VERSION,
        "report_kind": kind,
        "status": status,
        "compatibility": compatibility,
        "manifest": {
            "report_sha256": digest(report),
            "report_schema": report["schema"],
            "report_schema_version": report["schema_version"],
            "product": analysis["tool"],
            "root_bindings": bindings,
            "configuration": analysis["configuration"],
            "document_configurations": {
                key: value["configuration"] for key, value in bound.items()
            },
            "engine_metadata": [
                {key: engine[key] for key in ("name", "status", "details", "diagnostics")}
                for engine in analysis["engines"]
            ],
            "capture": run_manifest,
            "product_commit": run_manifest["product_commit"] if run_manifest else None,
            "missing_provenance": []
            if run_manifest
            else ["capture_manifest", "product_commit", "wall_clock_time"],
            "judgments_sha256": digest(judgments) if judgments is not None else None,
        },
        "summary": summary,
        "cases": cases,
        "engines": {
            engine: {
                "summary": summarize(rows[engine]),
                "cases": rows[engine],
                "strata": _strata(rows[engine]),
            }
            for engine in engine_names
        },
        "strata": _strata(cases),
        "contribution": contribution,
        "diagnostics": {
            "case_reasons": dict(Counter(reason for case in cases for reason in case["reasons"])),
            "ignored_outside_annotated_cases": dict(ignored),
            "label_states": dict(Counter(case["annotation"]["status"] for case in selected)),
            "label_annotation_states": dict(
                Counter(
                    label["annotation"]["status"] for case in selected for label in case["labels"]
                )
            ),
            "unannotated_category_candidates": sum(
                item["outcome"] == "not_evaluated" for case in cases for item in case["candidates"]
            ),
            "levels": {
                "final_localized": sum(len(case["candidates"]) for case in cases),
                "retained_localized_sources": {
                    name: sum(len(case["candidates"]) for case in rows[name])
                    for name in engine_names
                },
                "raw_nonlocalized_scoring": "not_evaluated_no_source_coordinates",
                "filtered_by_engine": dict(
                    Counter(item["engine"] for item in report.get("filtered", []))
                )
                if "filtered" in report
                else None,
                "rejected_by_engine": dict(
                    Counter(
                        item["engine"] for doc in analysis["documents"] for item in doc["rejected"]
                    )
                ),
            },
            "rejected": dict(
                Counter(item["reason"] for doc in analysis["documents"] for item in doc["rejected"])
            ),
            "filtered": len(report["filtered"]) if "filtered" in report else None,
            "unlocalized": len(report["unlocalized"]) if "unlocalized" in report else None,
            "independently_judged_harm": dict(
                Counter(
                    harm
                    for case in cases
                    for item in case["candidates"]
                    for harm in item["harm"] or []
                )
            ),
            "harm_not_evaluated_candidates": sum(
                item["harm"] is None for case in cases for item in case["candidates"]
            ),
            "limitations": [
                "scan viewed/selected totals are absent in check 1.0",
                "check sources are localized retained outputs, not all raw proposals",
                "filtered/unlocalized hypotheses have no reliable source ranges and cannot earn TP",
                "no independent semantic harm judge; risk reasons are diagnostics",
                "small strata: inspect n; no population inference",
            ],
        },
        "cost": {
            "wall_clock_ms": run_manifest["wall_clock_ms"] if run_manifest else None,
            "engines": [
                {key: engine[key] for key in ("name", "elapsed_ms", "statistics", "tokens")}
                for engine in analysis["engines"]
            ],
            "money": None,
            "resource_usage": None,
            "retries": None,
        },
    }
    validate(result, "result")
    return result
