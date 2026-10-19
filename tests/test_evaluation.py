"""Математика и контракты синтетических evals; не измерение языковой точности."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import JsonServer
from support.check_helpers import _config, _languagetool_config, languagetool_responder
from support.evaluation_helpers import add_label, make_corpus, write_source

from language_review.check import check_paths
from language_review.check_report import check_document
from language_review.config import load_config
from language_review.errors import InputError
from language_review.evaluation.cli import main, render
from language_review.evaluation.comparison import (
    check_expectation,
    compare,
    distribution,
    expectation,
)
from language_review.evaluation.contracts import (
    canonical,
    read_json,
    save_new,
    schema,
    validate,
)
from language_review.evaluation.corpus import (
    annotation_agreement,
    source_map,
    validate_corpus,
)
from language_review.evaluation.corpus import (
    test_hash as frozen_hash,
)
from language_review.evaluation.live import capture, materialize
from language_review.evaluation.reports import load_product_report
from language_review.evaluation.scoring import metrics, score
from language_review.review_report import review_document

BINDINGS = {"root-0001": "demo"}


def run_report(
    corpus: dict[str, Any],
    tmp_path: Path,
    server: JsonServer,
    replacements: dict[str, list[str]],
    review: bool = False,
) -> dict[str, Any]:
    root = tmp_path / "inputs"
    write_source(corpus, root)
    server.responder = languagetool_responder(replacements)
    config = load_config(_languagetool_config(tmp_path, server.url))
    result = check_paths([str(root)], config)
    return review_document(result) if review else check_document(result)


@pytest.fixture
def measured(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> tuple[dict[str, Any], dict[str, Any]]:
    corpus = make_corpus()
    add_label(corpus, "ошыбка", corrections=("ошибка", "опечатка"))
    add_label(corpus, "ашибка", corrections=("ошибка",))
    add_label(corpus, "Важные файл", "grammar", ("Важный файл",))
    report = run_report(
        corpus,
        tmp_path,
        json_server,
        {"ошыбка": ["ошибка"], "ашибка": ["ашибочка"], "Протокол": ["Протакол"]},
    )
    return corpus, report


def test_actual_check_detection_correction_and_denominators(
    measured: Any, json_server: JsonServer
) -> None:
    corpus, report = measured
    request_count = len(json_server.requests)
    result = score(corpus, report, BINDINGS, "dev")
    assert len(json_server.requests) == request_count  # offline: no additional requests
    summary = result["summary"]
    assert result["status"] == "PASS"
    assert result["report_kind"] == "synthetic_contract_check"
    assert summary["detection"] == metrics(2, 1, 1)
    assert summary["detection"]["precision"] == pytest.approx(2 / 3)
    assert summary["detection"]["f0_5"] == pytest.approx(2 / 3)
    assert summary["correction"] == metrics(1, 2, 2)
    assert summary["evaluated_applicable_cases"] == 1
    assert summary["fp_per_1000_evaluated_applicable_cases"] == 1000
    assert result["strata"]["category"]["grammar"]["pipeline_detection"] == metrics(0, 0, 1)
    assert result["engines"]["languagetool"]["summary"]["detection"] == metrics(2, 1, 1)
    assert len(result["contribution"]["languagetool"]["unique_final_tp"]) == 2
    assert score(corpus, report, BINDINGS, "dev") == result
    assert "synthetic_contract_check" in render(result)
    assert "n —" in render(result)


def test_duplicate_candidate_cannot_earn_second_tp(measured: Any) -> None:
    corpus, report = measured
    duplicate = copy.deepcopy(report["documents"][0]["findings"][0])
    duplicate["id"] = "find-" + "d" * 24
    report["documents"][0]["findings"].append(duplicate)
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["detection"] == metrics(2, 2, 1)
    assert sum(item["duplicate"] for item in result["cases"][0]["candidates"]) == 1


def test_unstable_product_ids_do_not_change_comparison(measured: Any) -> None:
    corpus, report = measured
    first = score(corpus, report, BINDINGS, "dev")
    for index, item in enumerate(report["documents"][0]["findings"]):
        item["id"] = "find-" + f"{index:024x}"
    second = score(corpus, report, BINDINGS, "dev")
    difference = compare(first, second)
    assert difference["new_fp"] == difference["new_fn"] == difference["changed_tp"] == []
    assert difference["summary_deltas"] == {}
    assert check_expectation(second, expectation(first))["status"] == "PASS"


def test_exact_category_and_range_are_required(measured: Any) -> None:
    corpus, report = measured
    corpus["cases"][0]["labels"][0]["category"] = "punctuation"
    corpus["cases"][0]["categories"].append("punctuation")
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["detection"] == metrics(1, 2, 2)
    source = source_map(corpus["documents"][0])
    corpus["cases"][0]["labels"][0]["category"] = "spelling"
    corpus["cases"][0]["labels"][0]["anchors"] = [
        source.range_dict(0, source.text.index("ошыбка") + len("ошыбка"))
    ]
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["needs_adjudication"] == 1
    assert result["cases"][0]["coverage"]["unresolved_gold"] == ["gold-1"]
    assert "gold-1" not in result["cases"][0]["tp"]
    assert "gold-1" not in result["cases"][0]["fn"]


def test_one_candidate_cannot_cover_two_gold_labels(measured: Any) -> None:
    corpus, report = measured
    label = copy.deepcopy(corpus["cases"][0]["labels"][0])
    label["id"] = "second-diagnosis"
    corpus["cases"][0]["labels"].append(label)
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["needs_adjudication"] == 1
    assert result["cases"][0]["tp"] == ["gold-2"]
    assert set(result["cases"][0]["coverage"]["unresolved_gold"]) == {"gold-1", "second-diagnosis"}


def test_alternative_replacement_unknown_and_mixed_corrections(measured: Any) -> None:
    corpus, report = measured
    corpus["cases"][0]["labels"][1]["corrections"] = []
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["unknown_corrections"] == 1
    assert result["summary"]["correction"] == metrics(1, 1, 1)
    finding = next(
        item for item in report["documents"][0]["findings"] if item["excerpt"] == "ошыбка"
    )
    suggestion = copy.deepcopy(finding["suggestions"][0])
    suggestion["edit"]["text"] = "новый смысл"
    finding["suggestions"].append(suggestion)
    result = score(corpus, report, BINDINGS, "dev")
    proposal = next(item for item in result["cases"][0]["candidates"] if item["label"] == "gold-1")
    assert proposal["correction"] == "needs_adjudication"
    assert proposal["outcome"] == "tp"


@pytest.mark.parametrize("status", ["draft", "disputed"])
def test_unconfirmed_annotation_cannot_enter_primary_metrics(measured: Any, status: str) -> None:
    corpus, report = measured
    corpus["cases"][0]["labels"][0]["annotation"]["status"] = status
    result = score(corpus, report, BINDINGS, "dev")
    assert result["status"] == "NOT_RUN"
    assert result["summary"]["eligible_cases"] == 0
    assert result["summary"]["pipeline_detection"] == metrics(0, 0, 0)
    assert result["cases"][0]["state"] == "not_evaluated"


def test_real_model_or_pipeline_labels_cannot_self_verify() -> None:
    corpus = make_corpus()
    corpus["documents"][0]["data_kind"] = "real"
    for method in ("model", "pipeline", "synthetic_contract"):
        corpus["cases"][0]["annotation"]["method"] = method
        with pytest.raises(InputError, match="независимого"):
            validate_corpus(corpus)
    corpus["cases"][0]["annotation"]["method"] = "human"
    assert validate_corpus(corpus) == corpus


@pytest.mark.parametrize(
    "mode", ["missing", "unavailable", "partial_extract", "category", "uncovered", "rejected"]
)
def test_incomplete_is_never_clean(measured: Any, mode: str) -> None:
    corpus, report = measured
    doc = report["documents"][0]
    doc["findings"] = []
    if mode == "missing":
        report["documents"] = []
    elif mode == "unavailable":
        doc["status"] = "unavailable"
        for fragment in doc["fragments"]:
            fragment["checks"]["languagetool"] = {
                "status": "skipped",
                "reason": "engine_unavailable",
            }
    elif mode == "partial_extract":
        doc["extraction_status"] = "partial"
    elif mode == "category":
        doc["configuration"]["categories"] = ["style"]
    elif mode == "uncovered":
        doc["fragments"] = []
    else:
        doc["rejected"] = [
            {
                "engine": "languagetool",
                "reason": "excerpt_ambiguous",
                "fragment_ids": [doc["fragments"][0]["id"]],
                "rule_id": None,
                "message": None,
                "excerpt": "ошыбка",
            }
        ]
    result = score(corpus, report, BINDINGS, "dev")
    assert result["status"] in {"PARTIAL", "NOT_RUN"}
    assert result["summary"]["evaluated_applicable_cases"] == 0
    assert result["summary"]["tn_clean_cases"] == 0
    assert result["summary"]["pipeline_detection"] == metrics(0, 0, 3)
    assert result["summary"]["fp_per_1000_evaluated_applicable_cases"] is None
    assert result["cases"][0]["reasons"]


@pytest.mark.parametrize(
    "encoding,bom",
    [("utf-8", None), ("utf-8", "utf-8"), ("utf-16-le", "utf-16-le"), ("utf-16-be", "utf-16-be")],
)
def test_coordinates_unicode_bom_crlf_and_repeated_quotes(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer, encoding: str, bom: str | None
) -> None:
    corpus = make_corpus("😀 Первая ошыбка.\r\nВторая ошыбка.\r\n", encoding=encoding, bom=bom)
    add_label(corpus, "ошыбка", corrections=("ошибка",))
    add_label(corpus, "ошыбка", corrections=("ошибка",), occurrence=1)
    report = run_report(corpus, tmp_path, json_server, {"ошыбка": ["ошибка"]})
    before = (tmp_path / "inputs" / "sample.md").read_bytes()
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["pipeline_detection"]["tp"] == 1
    assert result["summary"]["pipeline_detection"]["fn"] == 1
    first = report["documents"][0]["findings"][0]["range"]["start"]
    assert first["utf16_column"] == first["column"] + 1
    assert (tmp_path / "inputs" / "sample.md").read_bytes() == before
    report["documents"][0]["findings"][0]["range"]["start"]["column"] += 1
    with pytest.raises(InputError, match="координаты"):
        score(corpus, report, BINDINGS, "dev")


@pytest.mark.parametrize("mutation", ["source", "version", "quote", "root", "range", "edit", "ids"])
def test_invalid_product_reports_fail_closed(measured: Any, mutation: str) -> None:
    corpus, report = measured
    doc = report["documents"][0]
    if mutation == "source":
        doc["source_sha256"] = "0" * 64
    if mutation == "version":
        report["schema_version"] = "2.0"
    if mutation == "quote":
        doc["findings"][0]["excerpt"] = "forged"
    if mutation == "root":
        doc["root"] = "root-9999"
    if mutation == "range":
        doc["fragments"][0]["range"]["end"]["byte_offset"] += 1
    if mutation == "edit":
        doc["findings"][0]["suggestions"][0]["edit"]["original"] = "forged"
    if mutation == "ids":
        doc["findings"].append(copy.deepcopy(doc["findings"][0]))
    with pytest.raises(InputError):
        score(corpus, report, BINDINGS, "dev")


def test_actual_review_import_and_evidence_validation(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    corpus = make_corpus("Здесь ошыбка.")
    add_label(corpus, "ошыбка", corrections=("ошибка",))
    report = run_report(corpus, tmp_path, json_server, {"ошыбка": ["ошибка"]}, review=True)
    path = tmp_path / "review.json"
    save_new(path, report)
    imported = load_product_report(path)
    assert score(corpus, imported, BINDINGS, "dev")["summary"]["detection"] == metrics(1, 0, 0)
    report["items"][0]["evidence"][0]["source_excerpt"] = "forged"
    with pytest.raises(InputError):
        score(corpus, report, BINDINGS, "dev")


def test_split_leakage_freeze_and_annotation_agreement() -> None:
    corpus = make_corpus()
    second_doc = copy.deepcopy(corpus["documents"][0])
    second_doc["id"] = "doc-2"
    second_doc["repository"] = "another"
    second_case = copy.deepcopy(corpus["cases"][0])
    second_case.update(id="case-2", document="doc-2", split="test")
    corpus["documents"].append(second_doc)
    corpus["cases"].append(second_case)
    with pytest.raises(InputError, match="дубликата"):
        validate_corpus(corpus)
    corpus["cases"][1]["split"] = "dev"
    validate_corpus(corpus)
    corpus["cases"][0]["split"] = "test"
    corpus["cases"][1]["split"] = "test"
    corpus["split_policy"].update(test_frozen=True, test_sha256=frozen_hash(corpus))
    validate_corpus(corpus)
    corpus["cases"][0]["exceptions"].append("new answer")
    with pytest.raises(InputError, match="замороженный"):
        validate_corpus(corpus)
    first = make_corpus()
    second = copy.deepcopy(first)
    first["cases"][0]["annotation"]["method"] = "human"
    second["cases"][0]["annotation"]["method"] = "human"
    second["cases"][0]["annotation"]["actor"] = "independent-second"
    assert annotation_agreement(first, second)["agreement"] == 1
    second["cases"][0]["categories"] = ["spelling"]
    assert annotation_agreement(first, second)["conflicts"] == ["case-1"]
    second["cases"][0]["categories"] = first["cases"][0]["categories"]
    second["cases"][0]["exhaustive"] = False
    assert annotation_agreement(first, second)["conflicts"] == ["case-1"]
    second["cases"][0]["exhaustive"] = first["cases"][0]["exhaustive"]
    add_label(second, "ошыбка")
    second["cases"][0]["labels"][0]["annotation"]["method"] = "human"
    assert annotation_agreement(first, second)["conflicts"] == ["case-1"]
    same_actor = annotation_agreement(first, first)
    assert same_actor["agreement"] is None
    assert same_actor["excluded_same_actor"] == ["case-1"]
    second["cases"][0]["annotation"].update(status="draft", method="model")
    assert annotation_agreement(first, second)["agreement"] is None
    assert annotation_agreement(first, second)["excluded_nonindependent_method"] == ["case-1"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "../outside.md",
        "C:/secret.md",
        "folder\\name.md",
        "CON.txt",
        "file.md:stream",
        "folder./file.md",
    ],
)
def test_corpus_rejects_unsafe_paths(unsafe: str) -> None:
    corpus = make_corpus(path=unsafe)
    with pytest.raises(InputError, match="путь"):
        validate_corpus(corpus)


def test_zero_denominators_empty_split_and_clean_negative(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    corpus = make_corpus("Корректный текст не следует менять.")
    report = run_report(corpus, tmp_path, json_server, {})
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["tn_clean_cases"] == 1
    assert result["summary"]["detection"]["precision"] is None
    assert result["summary"]["detection"]["recall"] is None
    assert result["summary"]["detection"]["undefined"]
    assert score(corpus, report, BINDINGS, "test")["status"] == "NOT_RUN"


def test_comparison_regressions_compatibility_and_repeated_distribution(measured: Any) -> None:
    corpus, report = measured
    first = score(corpus, report, BINDINGS, "dev")
    doc = report["documents"][0]
    removed = next(item for item in doc["findings"] if item["excerpt"] == "ошыбка")
    doc["findings"].remove(removed)
    second = score(corpus, report, BINDINGS, "dev")
    difference = compare(first, second)
    assert difference["new_fn"] == ["case-1/gold-1"]
    assert difference["lost_tp"] == ["case-1/gold-1"]
    assert compare(second, first)["recovered_fn"] == ["case-1/gold-1"]
    assert check_expectation(second, expectation(first))["status"] == "FAIL"
    repeated = distribution([first, second])
    assert repeated["repetitions"] == 2
    assert repeated["pipeline_detection"]["recall"]["values"] == [2 / 3, 1 / 3]
    assert not repeated["independent_stability_proven"]
    for field in ("corpus_sha256", "annotation_version", "metric_version", "split"):
        incompatible = copy.deepcopy(second)
        incompatible["compatibility"][field] = (
            "0" * 64 if field == "corpus_sha256" else "test" if field == "split" else "2"
        )
        with pytest.raises(InputError, match="Несовместимы"):
            compare(first, incompatible)


def test_cli_import_export_offline_and_read_only(
    measured: Any, tmp_path: Path, capsys: Any
) -> None:
    corpus, report = measured
    corpus_path = tmp_path / "corpus.json"
    report_path = tmp_path / "report.json"
    save_new(corpus_path, corpus)
    save_new(report_path, report)
    exported = tmp_path / "export.json"
    assert main(["export", str(corpus_path), "--output", str(exported)]) == 0
    assert exported.read_bytes() == corpus_path.read_bytes()
    assert main(["validate", str(exported)]) == 0
    output = tmp_path / "scored.json"
    args = [
        "score",
        str(corpus_path),
        str(report_path),
        "--split",
        "dev",
        "--bind",
        "root-0001=demo",
        "--format",
        "json",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert read_json(output)["summary"]["detection"] == metrics(2, 1, 1)
    assert corpus_path.read_text("utf-8") == canonical(corpus)
    assert main(args) == 3  # exclusive-create, no overwrite
    expected = tmp_path / "expected.json"
    assert main(["regress", str(output), str(expected), "--record"]) == 0
    assert main(["regress", str(output), str(expected)]) == 0
    assert main(["compare", str(output), str(output), "--format", "json"]) == 0
    assert "new_fp" in capsys.readouterr().out
    corpus_path.write_text('{"schema":1,"schema":2}', encoding="utf-8")
    assert main(["validate", str(corpus_path)]) == 2


def test_live_public_cli_captures_real_reports_and_no_silent_success(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    corpus = make_corpus("Текст для проверки.")
    config = _config(tmp_path, '[check.languagetool]\nendpoint = "http://127.0.0.1:9"\n')
    result = capture(
        corpus, "dev", ["languagetool"], config, tmp_path / "runs", "a" * 40, repetitions=2
    )
    assert result["status"] == "PARTIAL"
    assert result["statuses"] == ["NOT_RUN", "NOT_RUN"]
    assert result["repetitions"] == 2
    report = load_product_report(tmp_path / "runs" / "report-1.json")
    assert report["analysis"]["engines"][0]["status"] == "unavailable"
    manifest = read_json(tmp_path / "runs" / "run-1.json")
    validate(manifest, "run")
    assert manifest["wall_clock_ms"] > 0
    assert str(tmp_path) not in canonical(manifest)


def test_materialization_contains_no_labels_and_keeps_bytes(tmp_path: Path) -> None:
    corpus = make_corpus("Правильный текст.\r\n", encoding="utf-16-le", bom="utf-16-le")
    destination = tmp_path / "export"
    assert materialize(corpus, "dev", destination) == BINDINGS
    assert (destination / "demo" / "sample.md").read_bytes() == source_map(
        corpus["documents"][0]
    ).raw
    assert len(list(destination.rglob("*.json"))) == 0


def test_packaged_check_schema_is_exact_existing_contract() -> None:
    expected = json.loads(
        (Path(__file__).parents[1] / "docs/check-report.schema.json").read_text("utf-8")
    )
    assert schema("check") == expected
    from jsonschema import Draft202012Validator

    for name in (
        "check",
        "corpus",
        "result",
        "run",
        "expectations",
        "comparison",
        "distribution",
        "judgments",
        "reference",
    ):
        Draft202012Validator.check_schema(schema(name))
