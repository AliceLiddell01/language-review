"""Регрессии независимой оценки: охват, решения, стабильные метки и происхождение."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import JsonServer
from support.evaluation_helpers import add_label, make_corpus
from test_evaluation import measured as measured
from test_evaluation import run_report
from test_evaluation_import import csv_rows, decision, set_csv

from language_review.errors import InputError
from language_review.evaluation.annotation_io import (
    DECISIONS,
    LABELS,
    LEGACY_LABELS,
    export_annotations,
    import_annotations,
)
from language_review.evaluation.cli import main
from language_review.evaluation.comparison import compare
from language_review.evaluation.contracts import canonical, digest, read_json, save_new, validate
from language_review.evaluation.corpus import (
    annotation_agreement,
    eligible,
    source_map,
    validate_corpus,
)
from language_review.evaluation.scoring import _coverage, score

BINDINGS = {"root-0001": "demo"}


def judgments_for(corpus: Any, report: Any, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "language-review.eval-judgments",
        "schema_version": "1.0",
        "version": "1",
        "corpus_sha256": digest(corpus),
        "report_sha256": digest(report),
        "entries": entries,
    }


def judgment(candidate: str, detection: str, label: str | None) -> dict[str, Any]:
    return {
        "case": "case-1",
        "candidate": candidate,
        "engine": None,
        "detection": detection,
        "label": label,
        "correction": "correct" if detection == "tp" else "incorrect",
        "harm": ["meaning"],
        "annotation": {
            "status": "independently_verified",
            "actor": "synthetic-reviewer",
            "method": "human",
            "evidence": "Синтетический ввод для проверки контракта, не реальная аттестация.",
            "history": [{"revision": "1", "actor": "synthetic-reviewer", "decision": "контроль"}],
        },
    }


@pytest.mark.parametrize("resolution", ["fp", "tp", "all_fp"])
def test_partial_judgments_release_only_resolved_gold_and_preserve_metrics(
    measured: Any, resolution: str
) -> None:
    corpus, report = measured
    source = source_map(corpus["documents"][0])
    labels = corpus["cases"][0]["labels"]
    for label, excerpt in zip(labels[:2], ("ошыбка", "ашибка"), strict=True):
        a = source.text.index(excerpt)
        label["anchors"] = [source.range_dict(a - 1, a + len(excerpt))]
    initial = score(corpus, report, BINDINGS, "dev")
    disputed = [
        c for c in initial["cases"][0]["candidates"] if c["outcome"] == "needs_adjudication"
    ]
    a = next(
        c for c in disputed if c["range"]["start"] == labels[0]["corrections"][0]["range"]["start"]
    )
    b = next(c for c in disputed if c is not a)
    assert initial["cases"][0]["coverage"]["unresolved_gold"] == ["gold-1", "gold-2"]
    entries = [
        judgment(
            a["key"], "tp" if resolution == "tp" else "fp", "gold-1" if resolution == "tp" else None
        )
    ]
    if resolution == "all_fp":
        entries.append(judgment(b["key"], "fp", None))
    result = score(
        corpus, report, BINDINGS, "dev", judgments=judgments_for(corpus, report, entries)
    )
    case, summary = result["cases"][0], result["summary"]
    assert case["coverage"]["unresolved_gold"] == ([] if resolution == "all_fp" else ["gold-2"])
    assert case["fn"] == (
        ["gold-3"]
        if resolution == "tp"
        else ["gold-1", "gold-2", "gold-3"]
        if resolution == "all_fp"
        else ["gold-1", "gold-3"]
    )
    assert (
        summary["pipeline_detection"]["tp"],
        summary["pipeline_detection"]["fp"],
        summary["pipeline_detection"]["fn"],
    ) == ((1, 1, 1) if resolution == "tp" else (0, 3, 3) if resolution == "all_fp" else (0, 2, 2))
    assert (
        summary["correction"]["tp"],
        summary["correction"]["fp"],
        summary["correction"]["fn"],
    ) == ((1, 1, 1) if resolution == "tp" else (0, 3, 3) if resolution == "all_fp" else (0, 2, 2))
    assert result["strata"]["category"]["spelling"]["pipeline_detection"]["fn"] == (
        0 if resolution == "tp" else 2 if resolution == "all_fp" else 1
    )
    assert result["diagnostics"]["independently_judged_harm"] == {"meaning": len(entries)}
    assert case["state"] == ("evaluated" if resolution == "all_fp" else "incomplete")
    assert result["status"] == ("PASS" if resolution == "all_fp" else "PARTIAL")
    assert ("matching:needs_adjudication" in case["reasons"]) is (resolution != "all_fp")
    # Решения pipeline не повышают независимые строки движка.
    assert result["engines"]["languagetool"]["cases"][0]["coverage"]["unresolved_gold"] == [
        "gold-1",
        "gold-2",
    ]


@pytest.mark.parametrize("detection", ["tp", "fp"])
def test_one_ambiguous_candidate_resolves_all_its_links_without_double_tp(
    measured: Any, detection: str
) -> None:
    corpus, report = measured
    extra = copy.deepcopy(corpus["cases"][0]["labels"][0])
    extra["id"] = "extra"
    corpus["cases"][0]["labels"].append(extra)
    initial = score(corpus, report, BINDINGS, "dev")
    candidate = next(
        c for c in initial["cases"][0]["candidates"] if c["outcome"] == "needs_adjudication"
    )
    assert candidate["potential_labels"] == ["extra", "gold-1"]
    entries = [judgment(candidate["key"], detection, "gold-1" if detection == "tp" else None)]
    result = score(
        corpus, report, BINDINGS, "dev", judgments=judgments_for(corpus, report, entries)
    )
    assert result["cases"][0]["coverage"]["unresolved_gold"] == []
    assert result["cases"][0]["fn"] == (
        ["extra", "gold-3"] if detection == "tp" else ["extra", "gold-1", "gold-3"]
    )
    if detection == "tp":
        other = next(c for c in initial["cases"][0]["candidates"] if c["label"] == "gold-2")
        entries.append(judgment(other["key"], "tp", "gold-1"))
        with pytest.raises(InputError, match="однозначность"):
            score(corpus, report, BINDINGS, "dev", judgments=judgments_for(corpus, report, entries))


def test_coverage_separates_nonapplicable_engine_diagnostics(measured: Any) -> None:
    corpus, report = measured
    doc = report["documents"][0]
    doc["configuration"]["engines"].append("llm")
    for fragment in doc["fragments"]:
        fragment["checks"]["llm"] = {"status": "skipped", "reason": "no_russian_text"}
    coverage, reasons = _coverage(corpus["cases"][0], corpus["documents"][0], doc, None)
    assert coverage["evaluation_ready"] is True
    assert coverage["blocking_reasons"] == reasons == []
    assert coverage["diagnostic_reasons"] == ["routing_or_engine:llm:no_russian_text"]
    solo, reasons = _coverage(corpus["cases"][0], corpus["documents"][0], doc, "llm")
    assert solo["evaluation_ready"] is False
    assert "before:no_applicable_checked_engine" in reasons


def csv_label(text: str, excerpt: str, label_id: str = "error-A") -> dict[str, str]:
    start = text.index(excerpt)
    return {
        "id": "case-1",
        "label_id": label_id,
        "start": str(start),
        "end": str(start + len(excerpt)),
        "quote": excerpt,
        "category": "spelling",
        "replace": "yes",
        "replacement": "ошибка",
        "note": "Синтетический ввод.",
        "anchors": "",
        "corrections": "",
    }


def test_csv_explicit_ids_survive_reorder_addition_and_range_edit(tmp_path: Path) -> None:
    corpus = make_corpus()
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    assert read_json(packet / "binding.json")["schema_version"] == "2.0"
    decision(packet, "errors")
    text = source_map(corpus["documents"][0]).text
    a, b = csv_label(text, "ошыбка"), csv_label(text, "ашибка", "error-B")

    def imported(rows: list[dict[str, str]]) -> dict[str, Any]:
        set_csv(packet / "labels.csv", LABELS, rows)
        return import_annotations(corpus, packet, "fixture-person", "контроль", True)

    first, second = imported([a, b]), imported([b, a])
    assert first == second

    def identities(value: Any) -> dict[str, Any]:
        return {label["id"]: label["anchors"] for label in value["cases"][0]["labels"]}

    source = source_map(corpus["documents"][0])
    assert (
        identities(first)
        == identities(second)
        == {
            "error-A": [source.range_dict(7, 13)],
            "error-B": [source.range_dict(22, 28)],
        }
    )
    expanded = imported([csv_label(text, "Протокол", "error-C"), b, a])
    assert {k: v for k, v in identities(expanded).items() if k != "error-C"} == identities(first)
    changed = csv_label(text, "Первая ошыбка.", "error-A")
    assert list(identities(imported([changed, b]))) == ["error-A", "error-B"]
    # Одинаковые области разных диагнозов не превращаются в одну ошибку.
    assert len(imported([a, {**a, "label_id": "different-diagnosis"}])["cases"][0]["labels"]) == 2


@pytest.mark.parametrize("label_id", ["", " ", "error-A"])
def test_csv_rejects_missing_or_repeated_label_identity(tmp_path: Path, label_id: str) -> None:
    corpus = make_corpus()
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet, "errors")
    text = source_map(corpus["documents"][0]).text
    set_csv(
        packet / "labels.csv",
        LABELS,
        [csv_label(text, "ошыбка"), csv_label(text, "ашибка", label_id)],
    )
    with pytest.raises(InputError, match="label_id"):
        import_annotations(corpus, packet, "fixture-person", "контроль", True)


def test_legacy_packet_retains_ids_and_untouched_saved_annotations(tmp_path: Path) -> None:
    corpus = make_corpus()
    add_label(corpus, "ошыбка")
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    binding = read_json(packet / "binding.json")
    binding.pop("schema_version")
    (packet / "binding.json").write_text(canonical(binding), encoding="utf-8")
    set_csv(packet / "labels.csv", LEGACY_LABELS, [])
    assert import_annotations(corpus, packet, "", "", False) == corpus
    decision(packet, "errors")
    row = csv_label(source_map(corpus["documents"][0]).text, "ошыбка")
    set_csv(packet / "labels.csv", LEGACY_LABELS, [{k: row[k] for k in LEGACY_LABELS}])
    old = import_annotations(corpus, packet, "fixture-person", "контроль", True)
    assert old["cases"][0]["labels"][0]["id"] == "human-1"
    validate_corpus(old)


def test_csv_alternatives_are_one_gold_label_and_roundtrip_without_loss(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    corpus = make_corpus("Первая ошыбка.")
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet, "errors")
    row = csv_label("Первая ошыбка.", "ошыбка")
    row["anchors"] = json.dumps([{"start": 0, "end": 13, "quote": "Первая ошыбка"}])
    row["corrections"] = json.dumps(
        [
            {"start": 7, "end": 13, "quote": "ошыбка", "replacement": "опечатка"},
            {"start": 0, "end": 13, "quote": "Первая ошыбка", "replacement": "Первая ошибка"},
        ]
    )
    set_csv(packet / "labels.csv", LABELS, [row])
    imported = import_annotations(corpus, packet, "fixture-person", "контроль", True)
    labels = imported["cases"][0]["labels"]
    assert len(labels) == 1 and labels[0]["id"] == "error-A"
    assert len(labels[0]["anchors"]) == 2
    assert [edit["text"] for edit in labels[0]["corrections"]] == [
        "ошибка",
        "опечатка",
        "Первая ошибка",
    ]
    report = run_report(imported, tmp_path, json_server, {"ошыбка": ["опечатка"]})
    result = score(imported, report, BINDINGS, "dev")
    assert (result["summary"]["detection"]["tp"], result["summary"]["detection"]["fn"]) == (1, 0)
    assert result["summary"]["correction"]["tp"] == 1
    path, output = tmp_path / "imported.json", tmp_path / "exported.json"
    save_new(path, imported)
    assert main(["export", str(path), "--output", str(output)]) == 0
    assert read_json(output) == imported


@pytest.mark.parametrize("replace,expected", [("no", []), ("yes", [""])])
def test_csv_distinguishes_absent_correction_and_empty_deletion(
    tmp_path: Path, replace: str, expected: list[str]
) -> None:
    corpus = make_corpus()
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet, "errors")
    row = csv_label(source_map(corpus["documents"][0]).text, "ошыбка")
    row.update(replace=replace, replacement="")
    set_csv(packet / "labels.csv", LABELS, [row])
    imported = import_annotations(corpus, packet, "fixture-person", "контроль", True)
    assert [edit["text"] for edit in imported["cases"][0]["labels"][0]["corrections"]] == expected


@pytest.mark.parametrize("status", ["draft", "independently_verified"])
@pytest.mark.parametrize("verdict", ["clean", "errors"])
@pytest.mark.parametrize("categories", [[], ["spelling"]])
def test_corpus_category_scope_cannot_certify_empty_clean(
    status: str, verdict: str, categories: list[str]
) -> None:
    corpus = make_corpus()
    case = corpus["cases"][0]
    if verdict == "errors":
        add_label(corpus, "ошыбка")
    case["categories"] = categories
    case["annotation"]["status"] = status
    if not categories and (status == "independently_verified" or verdict == "errors"):
        assert not eligible(case)
        with pytest.raises(InputError):
            validate_corpus(corpus)
    else:
        validate_corpus(corpus)
        assert eligible(case) is (status == "independently_verified")


@pytest.mark.parametrize("field", ["anchors", "corrections"])
@pytest.mark.parametrize(
    "start,end,blocked",
    [
        (7, 8, True),
        (6, 8, True),
        (6, 7, False),
        (10, 11, False),
        (7, 7, True),
        (8, 8, True),
        (10, 10, True),
        (6, 6, False),
        (11, 11, False),
    ],
)
def test_json_and_csv_share_protected_range_policy(
    tmp_path: Path, field: str, start: int, end: int, blocked: bool
) -> None:
    text = "Ошибка API конец."
    corpus = make_corpus(text)
    source = source_map(corpus["documents"][0])
    # API занимает [7,10); вставки на обеих границах консервативно запрещены.
    corpus["cases"][0]["protected_ranges"] = [source.range_dict(7, 10)]
    direct = copy.deepcopy(corpus)
    label = add_label(direct, "Ошибка")
    location = source.range_dict(start, end)
    label[field] = [location] if field == "anchors" else [{"range": location, "text": ""}]
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet, "errors")
    row = csv_label(text, "Ошибка")
    alternative = {"start": start, "end": end, "quote": text[start:end]}
    if field == "corrections":
        alternative["replacement"] = ""
    row[field] = json.dumps([alternative])
    set_csv(packet / "labels.csv", LABELS, [row])
    if blocked:
        with pytest.raises(InputError, match="защищённый"):
            validate_corpus(direct)
        with pytest.raises(InputError, match="защищённый"):
            import_annotations(corpus, packet, "fixture-person", "контроль", True)
    else:
        validate_corpus(direct)
        validate_corpus(import_annotations(corpus, packet, "fixture-person", "контроль", True))


@pytest.mark.parametrize(
    "left_status,right_status",
    [
        ("independently_verified", "independently_verified"),
        ("draft", "draft"),
        ("draft", "independently_verified"),
        ("disputed", "independently_verified"),
    ],
)
def test_agreement_separates_verified_from_unfinished_labels(
    left_status: str, right_status: str
) -> None:
    first = make_corpus()
    add_label(first, "ошыбка")
    second = copy.deepcopy(first)
    for corpus, actor in ((first, "annotator-a"), (second, "annotator-b")):
        case = corpus["cases"][0]
        for annotation in (case["annotation"], case["labels"][0]["annotation"]):
            annotation.update(actor=actor, method="human")
    first["cases"][0]["labels"][0]["annotation"]["status"] = left_status
    second["cases"][0]["labels"][0]["annotation"]["status"] = right_status
    result = annotation_agreement(first, second)
    verified = left_status == right_status == "independently_verified"
    assert result["paired_cases"] == int(verified)
    assert result["agreement"] == (1.0 if verified else None)
    assert result["diagnostic_agreement"]["paired_cases"] == int(not verified)
    assert result["diagnostic_agreement"]["agreement"] == (None if verified else 1.0)
    assert result["excluded_unverified"] == ([] if verified else ["case-1"])
    second["cases"][0]["categories"] = ["spelling"]
    conflict = annotation_agreement(first, second)
    assert (conflict if verified else conflict["diagnostic_agreement"])["conflicts"] == ["case-1"]


@pytest.mark.parametrize("target", ["case", "label"])
@pytest.mark.parametrize("defect", ["same_actor", "model", "pipeline", "draft"])
def test_agreement_checks_case_and_label_origin(target: str, defect: str) -> None:
    first = make_corpus()
    add_label(first, "ошыбка")
    second = copy.deepcopy(first)
    for corpus, actor in ((first, "annotator-a"), (second, "annotator-b")):
        case = corpus["cases"][0]
        for annotation in (case["annotation"], case["labels"][0]["annotation"]):
            annotation.update(actor=actor, method="human")
    case = second["cases"][0]
    annotation = case["annotation"] if target == "case" else case["labels"][0]["annotation"]
    annotation.update(
        {"actor": "annotator-a"}
        if defect == "same_actor"
        else {"status": "draft"}
        if defect == "draft"
        else {"method": defect}
    )
    result = annotation_agreement(first, second)
    assert result["paired_cases"] == 0 and result["agreement"] is None
    if defect == "draft":
        assert result["diagnostic_agreement"]["paired_cases"] == 1
    else:
        assert result["diagnostic_agreement"]["paired_cases"] == 0
        assert result[
            "excluded_same_actor" if defect == "same_actor" else "excluded_nonindependent_method"
        ] == ["case-1"]


def test_old_result_remains_readable_but_metric_versions_do_not_compare(measured: Any) -> None:
    corpus, report = measured
    current = score(corpus, report, BINDINGS, "dev")
    old = copy.deepcopy(current)
    old["scorer_version"] = old["compatibility"]["scorer_version"] = "1.0"
    old["metric_version"] = old["compatibility"]["metric_version"] = "exact-bijective-1"
    for item in old["cases"][0]["candidates"]:
        item.pop("potential_labels")
    validate(old, "result")
    with pytest.raises(InputError, match="Несовместимы"):
        compare(old, current)


def test_label_ids_are_scoped_to_case_and_old_judgments_remain_hash_bound(
    tmp_path: Path, measured: Any
) -> None:
    text = "Первая ошыбка. Вторая ашибка."
    corpus = make_corpus(text)
    source = source_map(corpus["documents"][0])
    boundary = text.index("Вторая")
    second = copy.deepcopy(corpus["cases"][0])
    corpus["cases"][0]["range"] = source.range_dict(0, boundary)
    second.update(id="case-2", range=source.range_dict(boundary, len(text)))
    corpus["cases"].append(second)
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decisions = csv_rows(packet / "decisions.csv")
    for row in decisions:
        row.update(decision="errors", exhaustive="yes", categories="spelling")
    set_csv(packet / "decisions.csv", DECISIONS, decisions)
    labels = [csv_label(text[:boundary], "ошыбка"), csv_label(text[boundary:], "ашибка")]
    labels[1]["id"] = "case-2"
    set_csv(packet / "labels.csv", LABELS, labels)
    imported = import_annotations(corpus, packet, "fixture-person", "контроль", True)
    assert [case["labels"][0]["id"] for case in imported["cases"]] == ["error-A", "error-A"]
    old_corpus, report = measured
    result = score(old_corpus, report, BINDINGS, "dev")
    candidate = next(c for c in result["cases"][0]["candidates"] if c["label"] == "gold-1")
    judgments = judgments_for(old_corpus, report, [judgment(candidate["key"], "tp", "gold-1")])
    assert score(old_corpus, report, BINDINGS, "dev", judgments=judgments)["cases"][0]["tp"] == [
        "gold-1",
        "gold-2",
    ]
    old_corpus["annotation_version"] = "new-version"
    with pytest.raises(InputError, match="другому корпусу"):
        score(old_corpus, report, BINDINGS, "dev", judgments=judgments)


@pytest.mark.parametrize(
    "alternative",
    [
        "{}",
        '[{"start":true,"end":1,"quote":"П"}]',
        '[{"start":0,"end":1,"quote":"неверно"}]',
        '[{"start":0,"start":1,"end":1,"quote":""}]',
    ],
)
def test_csv_rejects_malformed_or_forged_alternatives(tmp_path: Path, alternative: str) -> None:
    corpus = make_corpus()
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet, "errors")
    row = csv_label(source_map(corpus["documents"][0]).text, "ошыбка")
    row["anchors"] = alternative
    set_csv(packet / "labels.csv", LABELS, [row])
    with pytest.raises(InputError):
        import_annotations(corpus, packet, "fixture-person", "контроль", True)


@pytest.mark.parametrize(
    "location,blocked",
    [((0, 6), False), ((7, 9), True), ((9, 9), True), ((9, 10), True), ((10, 11), False)],
)
def test_zero_width_protection_is_conservative_for_gold(
    location: tuple[int, int], blocked: bool
) -> None:
    corpus = make_corpus("Ошибка API конец.")
    source = source_map(corpus["documents"][0])
    corpus["cases"][0]["protected_ranges"] = [source.range_dict(9, 9)]
    label = add_label(corpus, "Ошибка")
    label["anchors"] = [source.range_dict(*location)]
    if blocked:
        with pytest.raises(InputError, match="защищённый"):
            validate_corpus(corpus)
    else:
        validate_corpus(corpus)


def test_agreement_unpaired_cases_do_not_create_a_denominator() -> None:
    first = make_corpus()
    second = copy.deepcopy(first)
    second["cases"][0]["id"] = "different-case"
    result = annotation_agreement(first, second)
    assert result["unpaired"] == ["case-1", "different-case"]
    assert result["agreement"] is None and result["paired_cases"] == 0
    assert result["diagnostic_agreement"]["agreement"] is None
