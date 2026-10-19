"""Реальный конвейер, сохранённые фикстуры и ручная независимая adjudication."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import JsonServer
from support.check_helpers import (
    _config,
    _findings,
    _llm_finding,
    languagetool_responder,
    llama_responder,
)
from support.evaluation_helpers import add_label, make_corpus, write_source
from test_evaluation import measured as measured

from language_review.check import check_paths
from language_review.check_report import check_document
from language_review.config import load_config
from language_review.errors import InputError
from language_review.evaluation.cli import main, render
from language_review.evaluation.comparison import check_expectation, compare
from language_review.evaluation.contracts import digest, read_json, save_new
from language_review.evaluation.corpus import load_corpus, source_map, validate_corpus
from language_review.evaluation.live import capture
from language_review.evaluation.reports import load_product_report
from language_review.evaluation.scoring import metrics, score
from language_review.review_report import review_document

BINDINGS = {"root-0001": "demo"}


def test_metric_formulas_have_independent_asymmetric_oracle() -> None:
    actual = metrics(3, 1, 2)
    assert actual["precision"] == 0.75
    assert actual["recall"] == 0.6
    assert actual["f1"] == pytest.approx(6 / 9)
    assert actual["f0_5"] == pytest.approx(3.75 / 5.25)
    assert actual["denominators"] == {"precision": 4, "recall": 5, "f1": 9, "f0_5": 5.25}


def test_saved_synthetic_fixture_scores_through_actual_evaluator() -> None:
    folder = Path(__file__).parent / "fixtures" / "evaluation"
    corpus = load_corpus(folder / "corpus.json")
    report = load_product_report(folder / "report.json")
    result = score(corpus, report, BINDINGS, "dev")
    assert result["report_kind"] == "synthetic_contract_check"
    assert result["status"] == "PARTIAL"
    assert result["summary"]["pipeline_detection"]["tp"] == 2
    assert result["summary"]["pipeline_detection"]["fn"] == 9
    assert result["summary"]["not_evaluated_cases"] == 1
    assert check_expectation(result, read_json(folder / "expectations.json"))["status"] == "PASS"
    human = (folder / "report.ru.md").read_text("utf-8")
    assert human == "# Синтетический контроль оценщика\n\n" + render(result) + "\n"


def test_actual_two_engine_contribution_counts_hypothesis_once(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    corpus = make_corpus("Первая ошыбка. Важные файл.")
    add_label(corpus, "ошыбка", corrections=("ошибка",))
    add_label(corpus, "Важные файл", "grammar", ("Важный файл",))
    root = tmp_path / "inputs"
    write_source(corpus, root)
    lt = languagetool_responder({"ошыбка": ["ошибка"]})
    llm = llama_responder(
        lambda items: _findings(
            _llm_finding(items[0]["id"], "ошыбка", "ошибка"),
            _llm_finding(items[0]["id"], "Важные файл", "Важный файл", category="grammar"),
        )
    )
    json_server.responder = lambda request: (
        lt(request) if request.path.startswith("/v2/") else llm(request)
    )
    config = _config(
        tmp_path,
        f'[check]\nengines=["languagetool","llm"]\n'
        f'[check.languagetool]\nendpoint="{json_server.url}"\n'
        f'[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n',
    )
    report = review_document(check_paths([str(root)], load_config(config)))
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["detection"]["tp"] == 2
    assert result["summary"]["detection"]["fp"] == 0
    assert result["engines"]["languagetool"]["summary"]["detection"]["tp"] == 1
    assert result["engines"]["llm"]["summary"]["detection"]["tp"] == 2
    assert result["contribution"]["llm"]["unique_final_tp"] == [["case-1", "gold-2"]]
    assert result["contribution"]["languagetool"]["shared_final_tp"] == [["case-1", "gold-1"]]
    assert result["manifest"]["engine_metadata"][1]["details"]["prompt_template_sha256"]


@pytest.mark.parametrize(
    "text,enabled,checked",
    [
        ("The file was not found.", True, True),
        ("Failed!", True, False),
        ("API_ID_1", True, False),
        ("The file was not found.", False, False),
    ],
)
def test_english_routing_uses_real_public_check_contract(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    text: str,
    enabled: bool,
    checked: bool,
) -> None:
    corpus = make_corpus(text, "message.txt")
    corpus["cases"][0]["categories"] = ["untranslated_english"]
    add_label(corpus, text, "untranslated_english")
    root = tmp_path / "inputs"
    write_source(corpus, root)
    json_server.responder = llama_responder(_findings())
    category = 'categories=["untranslated_english"]' if enabled else 'categories=["grammar"]'
    config = _config(
        tmp_path,
        f'[check]\nengines=["llm"]\n{category}\n'
        f'[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n',
    )
    report = check_document(check_paths([str(root)], load_config(config)))
    result = score(corpus, report, BINDINGS, "dev")
    assert result["cases"][0]["coverage"]["checked"] is checked
    assert result["summary"]["pipeline_detection"]["fn"] == 1
    assert result["summary"]["tn_clean_cases"] == 0
    assert (
        any(request.path == "/v1/chat/completions" for request in json_server.requests) is checked
    )


def test_review_filtered_unlocalized_and_negation_rejection_are_diagnostic(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    corpus = make_corpus("Функция не изменяет файлы.\n", "message.txt")
    corpus["cases"][0]["categories"] = ["style"]
    root = tmp_path / "inputs"
    write_source(corpus, root)
    json_server.responder = llama_responder(
        lambda items: _findings(
            _llm_finding(items[0]["id"], "не изменяет", "изменяет", category="style"),
            _llm_finding(items[0]["id"], "несуществующая цитата", "текст", category="style"),
            _llm_finding(items[0]["id"], "Функция", "Операция", category="spelling"),
        )
    )
    config = _config(
        tmp_path,
        f'[check]\nengines=["llm"]\ncategories=["style"]\n'
        f'[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n',
    )
    result = score(
        corpus, review_document(check_paths([str(root)], load_config(config))), BINDINGS, "dev"
    )
    assert result["diagnostics"]["filtered"] == 1
    assert result["diagnostics"]["unlocalized"] == 1
    assert result["summary"]["pipeline_detection"]["tp"] == 0
    assert result["summary"]["overediting"] == 1
    assert result["cases"][0]["candidates"][0]["edits"] == []
    assert "negation_changed" in result["cases"][0]["candidates"][0]["risks"]
    assert result["diagnostics"]["harm_not_evaluated_candidates"] == 1


def test_independent_judgments_resolve_matching_and_measure_harm(measured: Any) -> None:
    corpus, report = measured
    original = score(corpus, report, BINDINGS, "dev")
    candidate = next(
        item for item in original["cases"][0]["candidates"] if item["label"] == "gold-2"
    )
    annotation = {
        "status": "independently_verified",
        "actor": "external-reviewer",
        "method": "human",
        "evidence": "Explicit synthetic adjudication contract, not real independent research",
        "history": [{"revision": "1", "actor": "external-reviewer", "decision": "reviewed"}],
    }
    judgments = {
        "schema": "language-review.eval-judgments",
        "schema_version": "1.0",
        "version": "1",
        "corpus_sha256": digest(corpus),
        "report_sha256": digest(report),
        "entries": [
            {
                "case": "case-1",
                "candidate": candidate["key"],
                "engine": None,
                "detection": "tp",
                "label": "gold-2",
                "correction": "incorrect",
                "harm": ["meaning", "negation", "technical_tokens"],
                "annotation": annotation,
            }
        ],
    }
    result = score(corpus, report, BINDINGS, "dev", judgments=judgments)
    assert result["diagnostics"]["independently_judged_harm"] == {
        "meaning": 1,
        "negation": 1,
        "technical_tokens": 1,
    }
    assert result["diagnostics"]["harm_not_evaluated_candidates"] == 2
    with pytest.raises(InputError, match="Несовместимы"):
        compare(original, result)
    judgments["entries"][0]["annotation"]["method"] = "model"
    with pytest.raises(InputError, match="независимого"):
        score(corpus, report, BINDINGS, "dev", judgments=judgments)
    judgments["entries"][0]["annotation"]["method"] = "human"
    judgments["report_sha256"] = "0" * 64
    with pytest.raises(InputError, match="другому"):
        score(corpus, report, BINDINGS, "dev", judgments=judgments)


def test_run_manifest_must_match_exact_report_and_corpus(measured: Any) -> None:
    corpus, report = measured
    manifest = {
        "schema": "language-review.eval-run",
        "schema_version": "1.0",
        "corpus_sha256": digest(corpus),
        "split": "dev",
        "report_sha256": digest(report),
        "root_bindings": BINDINGS,
        "product_commit": None,
        "configuration_sha256": None,
        "wall_clock_ms": None,
        "exit_code": None,
        "capture": "saved_report",
        "parameters": {},
        "repetition": 1,
    }
    assert score(corpus, report, BINDINGS, "dev", manifest)["status"] == "PASS"
    manifest["report_sha256"] = "0" * 64
    with pytest.raises(InputError, match="Манифест"):
        score(corpus, report, BINDINGS, "dev", manifest)


def test_unannotated_category_is_not_false_positive_or_false_clean(measured: Any) -> None:
    corpus, report = measured
    corpus["cases"][0]["categories"] = ["grammar"]
    corpus["cases"][0]["labels"] = corpus["cases"][0]["labels"][2:]
    result = score(corpus, report, BINDINGS, "dev")
    assert result["summary"]["detection"]["fp"] == 0
    assert result["diagnostics"]["unannotated_category_candidates"] == 3
    assert all(item["outcome"] == "not_evaluated" for item in result["cases"][0]["candidates"])
    corpus["cases"][0].update(verdict="clean", labels=[])
    clean = score(corpus, report, BINDINGS, "dev")
    assert clean["summary"]["tn_clean_cases"] == 1  # only the annotated grammar scope


def test_manual_resolution_preserves_bijection_and_recovers_ambiguous_gold(measured: Any) -> None:
    corpus, report = measured
    source = source_map(corpus["documents"][0])
    corpus["cases"][0]["labels"][0]["anchors"] = [
        source.range_dict(0, source.text.index("ошыбка") + len("ошыбка"))
    ]
    unresolved = score(corpus, report, BINDINGS, "dev")
    candidate = next(
        item
        for item in unresolved["cases"][0]["candidates"]
        if item["outcome"] == "needs_adjudication"
    )
    annotation = {
        "status": "independently_verified",
        "actor": "manual-contract-reviewer",
        "method": "human",
        "evidence": "Synthetic explicit equivalence of the exact source ranges",
        "history": [
            {"revision": "1", "actor": "manual-contract-reviewer", "decision": "same diagnosis"}
        ],
    }
    entry = {
        "case": "case-1",
        "candidate": candidate["key"],
        "engine": None,
        "detection": "tp",
        "label": "gold-1",
        "correction": "correct",
        "harm": [],
        "annotation": annotation,
    }
    judgments = {
        "schema": "language-review.eval-judgments",
        "schema_version": "1.0",
        "version": "1",
        "corpus_sha256": digest(corpus),
        "report_sha256": digest(report),
        "entries": [entry],
    }
    resolved = score(corpus, report, BINDINGS, "dev", judgments=judgments)
    assert resolved["status"] == "PASS"
    assert resolved["cases"][0]["tp"] == ["gold-1", "gold-2"]
    assert resolved["cases"][0]["coverage"]["unresolved_gold"] == []
    other = next(item for item in unresolved["cases"][0]["candidates"] if item["label"] == "gold-2")
    judgments["entries"].append({**entry, "candidate": other["key"]})
    with pytest.raises(InputError, match="однозначность"):
        score(corpus, report, BINDINGS, "dev", judgments=judgments)


def test_init_freeze_and_materialize_are_public_read_only_workflows(
    tmp_path: Path, capsys: Any
) -> None:
    source = tmp_path / "sample.rs"
    raw = b"// Test fixture.\r\n"
    source.write_bytes(raw)
    draft_path = tmp_path / "draft.json"
    assert (
        main(
            [
                "init",
                str(source),
                "--repository",
                "permitted",
                "--path",
                "src/sample.rs",
                "--origin",
                "authored synthetic test",
                "--rights-basis",
                "test authorship",
                "--data-kind",
                "synthetic",
                "--output",
                str(draft_path),
            ]
        )
        == 0
    )
    corpus = load_corpus(draft_path)
    assert corpus["cases"][0]["annotation"]["status"] == "draft"
    assert not corpus["cases"][0]["exhaustive"]
    assert source.read_bytes() == raw
    corpus["cases"][0]["split"] = "test"
    test_path = tmp_path / "test.json"
    save_new(test_path, corpus)
    frozen = tmp_path / "frozen.json"
    assert main(["freeze-test", str(test_path), "--output", str(frozen)]) == 0
    assert load_corpus(frozen)["split_policy"]["test_frozen"]
    exported = tmp_path / "materialized"
    assert main(["materialize", str(frozen), "--split", "test", "--output", str(exported)]) == 0
    assert (exported / "permitted" / "src" / "sample.rs").read_bytes() == raw
    assert len(list(exported.rglob("*.*"))) == 1
    assert source.read_bytes() == raw
    assert "root-0001" in capsys.readouterr().out


def test_timeout_is_not_a_successful_live_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(command: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timed_out)
    result = capture(
        make_corpus(), "dev", ["vale"], None, tmp_path / "timeout", "a" * 40, timeout=0.1
    )
    assert result["status"] == "NOT_RUN"
    assert result["repetitions"] == 0
    assert result["pipeline_detection"]["recall"] == {
        "values": [],
        "n": 0,
        "mean": None,
        "min": None,
        "max": None,
    }
    assert read_json(tmp_path / "timeout" / "not-run-1.json")["reason"] == "public_check_timeout"


@pytest.mark.parametrize(
    "mutation", ["hash", "bom", "overlap", "unscoped_label", "rights", "schema"]
)
def test_corpus_integrity_rejects_invalid_annotations(mutation: str) -> None:
    corpus = make_corpus()
    if mutation == "hash":
        corpus["documents"][0]["sha256"] = "0" * 64
    if mutation == "bom":
        corpus["documents"][0]["bom"] = "utf-8"
    if mutation == "overlap":
        second = copy.deepcopy(corpus["cases"][0])
        second["id"] = "second"
        corpus["cases"].append(second)
    if mutation == "unscoped_label":
        add_label(corpus, "ошыбка", "style")
    if mutation == "rights":
        corpus["documents"][0]["rights"]["use"] = "unverified"
    if mutation == "schema":
        corpus["schema_version"] = "2.0"
    with pytest.raises(InputError):
        validate_corpus(corpus)


def test_report_kind_requires_independent_real_case_confirmation(measured: Any) -> None:
    corpus, report = measured
    corpus["documents"][0]["data_kind"] = "real"
    case = corpus["cases"][0]
    case["annotation"].update(status="draft", method="model")
    for label in case["labels"]:
        label["annotation"].update(status="draft", method="model")
    result = score(corpus, report, BINDINGS, "dev")
    assert result["report_kind"] == "exploratory"
    assert result["status"] == "NOT_RUN"
    assert result["summary"]["eligible_cases"] == 0


def test_partial_pipeline_keeps_healthy_engine_metrics_separate(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    corpus = make_corpus("Здесь ошыбка.")
    add_label(corpus, "ошыбка", corrections=("ошибка",))
    root = tmp_path / "inputs"
    write_source(corpus, root)
    lt = languagetool_responder({"ошыбка": ["ошибка"]})
    json_server.responder = lambda request: (
        lt(request) if request.path.startswith("/v2/") else (503, {"error": "unavailable"})
    )
    config = _config(
        tmp_path,
        f'[check]\nengines=["languagetool","llm"]\n'
        f'[check.languagetool]\nendpoint="{json_server.url}"\n'
        f'[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n',
    )
    result = score(
        corpus, check_document(check_paths([str(root)], load_config(config))), BINDINGS, "dev"
    )
    assert result["status"] == "PARTIAL"
    assert result["summary"]["coverage"]["checked"] == 1
    assert result["summary"]["coverage"]["fully_checked"] == 0
    assert result["summary"]["evaluated_applicable_cases"] == 0
    assert result["engines"]["languagetool"]["summary"]["detection"]["tp"] == 1
    assert result["engines"]["llm"]["summary"]["evaluated_applicable_cases"] == 0


def test_comparison_exposes_new_fp_changed_tp_coverage_and_engine_time(measured: Any) -> None:
    corpus, report = measured
    first = score(corpus, report, BINDINGS, "dev")
    candidate = copy.deepcopy(first)
    candidate["cases"][0]["candidates"].append(
        {**candidate["cases"][0]["candidates"][0], "key": "new-unique-fp", "outcome": "fp"}
    )
    true_positive = next(
        item for item in candidate["cases"][0]["candidates"] if item["outcome"] == "tp"
    )
    true_positive["key"] = "changed-suggestion"
    candidate["cases"][0]["coverage"]["missing_characters"] = 2
    candidate["cost"]["wall_clock_ms"] = 30
    candidate["cost"]["engines"][0]["elapsed_ms"] = first["cost"]["engines"][0]["elapsed_ms"] + 5
    difference = compare(first, candidate)
    assert difference["new_fp"] == ["case-1/new-unique-fp"]
    assert difference["changed_tp"] == ["case-1/" + true_positive["label"]]
    assert difference["coverage_changes"]["case-1.coverage.missing_characters"]["delta"] == 2
    assert difference["time"]["engine_elapsed_ms.languagetool"]["delta"] == pytest.approx(5)
    corpus["cases"][0]["profile"] = "expected-but-not-selected"
    wrong_profile = score(corpus, report, BINDINGS, "dev")
    assert "before:profile_mismatch" in wrong_profile["cases"][0]["reasons"]
    assert wrong_profile["summary"]["evaluated_applicable_cases"] == 0
