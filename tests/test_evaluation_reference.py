"""Синтетические пары проверяют ограниченный протокол совпадения строк, не gold."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import JsonServer
from support.check_helpers import _languagetool_config, languagetool_responder

from language_review.check import check_paths
from language_review.check_report import check_document
from language_review.config import load_config
from language_review.errors import InputError
from language_review.evaluation import source_import
from language_review.evaluation.cli import main
from language_review.evaluation.contracts import canonical, digest, read_json, save_new
from language_review.evaluation.corpus import eligible, load_corpus, source_map
from language_review.evaluation.live import materialize
from language_review.evaluation.reference import change_shape, reference_score
from language_review.evaluation.scoring import score


def bundle_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    correction: str,
    pair_format: str = "plain",
    extra_pairs: tuple[tuple[str, str], ...] = (),
) -> tuple[Path, dict[str, Any]]:
    recipe = copy.deepcopy(source_import.recipes()["sage-github-ru"])
    files = {
        "README.md": b"Synthetic license fixture",
        "data/GitHubTypoCorpusRu/test.json": (
            "".join(
                json.dumps(
                    {"source": original, "correction": replacement, "domain": "GitHubTypoCorpusRu"},
                    ensure_ascii=False,
                )
                + "\n"
                for original, replacement in ((text, correction), *extra_pairs)
            )
        ).encode(),
    }
    recipe["files"] = [
        {"path": p, "sha256": hashlib.sha256(b).hexdigest()} for p, b in files.items()
    ]
    monkeypatch.setattr(source_import, "recipes", lambda: {recipe["id"]: recipe})
    monkeypatch.setattr(
        source_import,
        "_download",
        lambda url: next(b for p, b in files.items() if url.endswith("/" + p)),
    )
    bundle = tmp_path / "bundle"
    source_import.import_source(recipe["id"], bundle, pair_format=pair_format)
    return bundle, load_corpus(bundle / "corpus.json")


def capture_pair(
    tmp_path: Path, corpus: dict[str, Any], server: JsonServer, replacements: dict[str, list[str]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    roots = materialize(corpus, "test", tmp_path / "inputs")
    server.responder = languagetool_responder(replacements)
    config = load_config(_languagetool_config(tmp_path, server.url))
    report = check_document(
        check_paths([str(tmp_path / "inputs" / repo) for repo in roots.values()], config)
    )
    return report, {
        "schema": "language-review.eval-run",
        "schema_version": "1.0",
        "corpus_sha256": digest(corpus),
        "report_sha256": digest(report),
        "root_bindings": roots,
        "split": "test",
        "product_commit": None,
        "configuration_sha256": None,
        "wall_clock_ms": None,
        "exit_code": None,
        "capture": "saved_report",
        "parameters": {},
        "repetition": 1,
    }


@pytest.mark.parametrize(
    "original,correction,replacements,exact",
    [
        ("Здесь ашибка.", "Здесь ошибка.", {"ашибка": ["ошибка"]}, True),
        ("Здесь ашибка.", "Здесь ошибка!", {"ашибка": ["ошибка"]}, False),
        (
            "Здесь ашибка и апечатка.",
            "Здесь ошибка и опечатка.",
            {"ашибка": ["ошибка"], "апечатка": ["опечатка"]},
            True,
        ),
        ("Пример работает.", "Пример работает.", {}, True),
        ("Букваа здесь.", "Буква здесь.", {"Букваа": ["Буква"]}, True),
    ],
)
def test_reference_exact_string_agreement_never_promotes_gold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
    original: str,
    correction: str,
    replacements: dict[str, list[str]],
    exact: bool,
) -> None:
    bundle, corpus = bundle_pair(tmp_path, monkeypatch, original, correction)
    report, manifest = capture_pair(tmp_path, corpus, json_server, replacements)
    before = canonical(corpus)
    result = reference_score(corpus, report, bundle, "test", manifest)
    summary = result["scopes"]["pipeline"]["summary"]
    assert (summary["evaluated"], summary["denominator"], summary["exact_matches"]) == (
        1,
        1,
        int(exact),
    )
    assert summary["exact_match_rate"] == float(exact)
    assert summary["human_category_counts"] == {}
    assert result["report_kind"] == "external_reference_assessment"
    assert canonical(reference_score(corpus, report, bundle, "test", manifest)) == canonical(result)
    assert canonical(corpus) == before and not eligible(corpus["cases"][0])
    baseline = score(corpus, report, manifest["root_bindings"], "test", manifest)
    assert baseline["report_kind"] == "exploratory"
    assert baseline["summary"]["detection"]["precision"] is None
    assert baseline["summary"]["eligible_cases"] == 0
    assert "tp" not in summary and "semantic_harm" not in summary


def test_reference_alternatives_never_choose_using_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    bundle, corpus = bundle_pair(tmp_path, monkeypatch, "Здесь ашибка.", "Здесь ошибка.")
    report, manifest = capture_pair(
        tmp_path, corpus, json_server, {"ашибка": ["ошибка", "опечатка"]}
    )
    result = reference_score(corpus, report, bundle, "test", manifest)
    summary = result["scopes"]["pipeline"]["summary"]
    assert summary["applicable"] == summary["undetermined"] == 1
    assert summary["denominator"] == 0 and summary["exact_match_rate"] is None
    assert summary["status"] == "NOT_RUN"
    assert summary["reasons"] == {"multiple_candidate_alternatives": 1}


def test_reference_overlapping_edits_are_undetermined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    bundle, corpus = bundle_pair(tmp_path, monkeypatch, "Здесь ашибка.", "Тут ошибка.")
    report, manifest = capture_pair(
        tmp_path, corpus, json_server, {"ашибка": ["ошибка"], "Здесь ашибка": ["Тут ошибка"]}
    )
    result = reference_score(corpus, report, bundle, "test", manifest)
    summary = result["scopes"]["pipeline"]["summary"]
    assert summary["reasons"] == {"conflicting_candidate_edits": 1}
    assert summary["denominator"] == 0 and summary["exact_match_rate"] is None


@pytest.mark.parametrize("pair_format,denominator", [("markdown", 0), ("plain", 1)])
def test_indented_unknown_source_field_uses_explicit_plain_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
    pair_format: str,
    denominator: int,
) -> None:
    bundle, corpus = bundle_pair(
        tmp_path, monkeypatch, '        "Здесь ашибка.",', '        "Здесь ошибка.",', pair_format
    )
    report, manifest = capture_pair(tmp_path, corpus, json_server, {"ашибка": ["ошибка"]})
    result = reference_score(corpus, report, bundle, "test", manifest)
    summary = result["scopes"]["pipeline"]["summary"]
    assert summary["denominator"] == denominator
    assert summary["exact_match_rate"] == (1.0 if denominator else None)
    assert source_map(corpus["documents"][0]).text == '        "Здесь ашибка.",'
    if not denominator:
        assert result["scopes"]["pipeline"]["pairs"][0]["coverage"]["extracted"] is False


@pytest.mark.parametrize("mutation", ["reference", "license", "upstream", "manifest", "document"])
def test_reference_provenance_and_exact_input_tampering_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
    mutation: str,
) -> None:
    bundle, corpus = bundle_pair(tmp_path, monkeypatch, "Здесь ашибка.", "Здесь ошибка.")
    report, manifest = capture_pair(tmp_path, corpus, json_server, {})
    if mutation == "manifest":
        manifest["report_sha256"] = "0" * 64
    elif mutation == "document":
        corpus["documents"][0]["origin"] = "Changed provenance"
        manifest["corpus_sha256"] = digest(corpus)
    elif mutation == "reference":
        path = bundle / "references.json"
        value = read_json(path)
        value[corpus["cases"][0]["id"]]["correction"] = "Другой ответ."
        path.write_text(canonical(value), encoding="utf-8")
    else:
        path = (
            bundle
            / "upstream"
            / ("README.md" if mutation == "license" else "data/GitHubTypoCorpusRu/test.json")
        )
        path.write_bytes(path.read_bytes() + b" ")
    if mutation == "document":
        result = reference_score(corpus, report, bundle, "test", manifest)
        assert result["scopes"]["pipeline"]["summary"]["rejected"] == 1
    else:
        with pytest.raises(InputError):
            reference_score(corpus, report, bundle, "test", manifest)


@pytest.mark.parametrize(
    "text,correction,shapes",
    [
        ("API v1.", "API v2.", {"technical_token_changed"}),
        ("Слово.", "Слово!", {"punctuation_only"}),
        ("Ашибка.", "Ошибка!", {"multiple_changes"}),
        ("Тест.", "Тест.", {"unchanged_not_attested_clean"}),
        ("Тест.", "Тесты.", {"insert"}),
        ("Тесты.", "Тест.", {"delete"}),
        ("а" * 20001, "б", {"size_limit"}),
    ],
    ids=["technical", "punctuation", "mixed", "unchanged", "insertion", "deletion", "size"],
)
def test_change_shapes_are_diagnostics_without_inferred_gold_categories(
    text: str,
    correction: str,
    shapes: set[str],
) -> None:
    value = change_shape(text, correction)
    assert set(value["shapes"]) >= shapes
    assert "category" not in value and "anchors" not in value
    if text == correction:
        assert value["proposal_status"] == "unchanged_not_attested_clean"


def test_reference_cli_saves_same_canonical_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    bundle, corpus = bundle_pair(tmp_path, monkeypatch, "Здесь ашибка.", "Здесь ошибка.")
    report, manifest = capture_pair(tmp_path, corpus, json_server, {"ашибка": ["ошибка"]})
    report_path, manifest_path, output = (
        tmp_path / p for p in ["report.json", "manifest.json", "assessment.json"]
    )
    save_new(report_path, report)
    save_new(manifest_path, manifest)
    args = [
        "reference-score",
        str(bundle / "corpus.json"),
        str(report_path),
        "--source-bundle",
        str(bundle),
        "--run-manifest",
        str(manifest_path),
        "--split",
        "test",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert read_json(output) == reference_score(corpus, report, bundle, "test", manifest)
    assert main(args) == 3  # Перезапись запрещена.
    (bundle / "provenance.json").write_text(
        canonical({"schema": "language-review.eval-source", "schema_version": "1.0"}),
        encoding="utf-8",
    )
    invalid_args = args.copy()
    invalid_args[-1] = str(tmp_path / "invalid-assessment.json")
    assert main(invalid_args) == 2


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("partial_extract", "before:extraction_partial"),
        ("profile", "before:profile_mismatch"),
        ("category", "before:category_disabled:grammar"),
        ("rejected", "after:rejected:excerpt_ambiguous"),
        ("partial", "document:partial"),
        ("error", "document:error"),
        ("missing", "before:document_missing_scan_or_selection_unknown"),
        ("engine", "routing_or_engine:languagetool:engine_not_selected"),
        ("uncovered", "before:extract_uncovered"),
        ("success", None),
    ],
)
def test_reference_requires_complete_coverage_not_only_checked_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
    defect: str,
    reason: str | None,
) -> None:
    bundle, corpus = bundle_pair(tmp_path, monkeypatch, "Здесь ашибка.", "Здесь ошибка.")
    report, manifest = capture_pair(tmp_path, corpus, json_server, {"ашибка": ["ошибка"]})
    doc = report["documents"][0]
    if defect == "partial_extract":
        doc["extraction_status"] = "partial"
    elif defect == "profile":
        doc["profile"] = "another-profile"
        # Ожидание профиля фиксируется до source import, не подделкой reference.
        original = source_import._case

        def with_profile(*args: Any, **kwargs: Any) -> dict[str, Any]:
            value = original(*args, **kwargs)
            value["profile"] = "expected-profile"
            return value

        monkeypatch.setattr(source_import, "_case", with_profile)
        # Новый bundle сохраняет настоящее pinned соответствие case/reference.
        other = tmp_path / "profile"
        other.mkdir()
        bundle, corpus = bundle_pair(other, monkeypatch, "Здесь ашибка.", "Здесь ошибка.")
    elif defect == "category":
        doc["configuration"]["categories"] = [
            category for category in corpus["cases"][0]["categories"] if category != "grammar"
        ]
    elif defect == "rejected":
        doc["rejected"].append(
            {
                "engine": "languagetool",
                "reason": "excerpt_ambiguous",
                "fragment_ids": [doc["fragments"][0]["id"]],
                "rule_id": None,
                "message": None,
                "excerpt": "ашибка",
            }
        )
    elif defect in {"partial", "error"}:
        doc["status"] = defect
    elif defect == "missing":
        report["documents"] = []
    elif defect == "engine":
        doc["configuration"]["engines"] = ["languagetool"]
        for fragment in doc["fragments"]:
            fragment["checks"] = {}
    elif defect == "uncovered":
        doc["fragments"] = []
        doc["findings"] = []
    manifest.update(corpus_sha256=digest(corpus), report_sha256=digest(report))
    result = reference_score(corpus, report, bundle, "test", manifest)
    for name in ("pipeline", "languagetool"):
        scope = result["scopes"][name]
        summary, pair = scope["summary"], scope["pairs"][0]
        assert summary["denominator"] == int(reason is None)
        assert summary["rejected"] == 0
        assert summary["status"] == ("PASS" if reason is None else "NOT_RUN")
        assert pair["state"] == ("evaluated" if reason is None else "undetermined")
        if reason:
            assert pair["reason"] == "incomplete_coverage"
            assert reason in pair["coverage_reasons"]
            assert pair["exact"] is None
        else:
            assert pair["exact"] is True
    # Неподтверждённые внешние пары по-прежнему не становятся human gold.
    ordinary = score(corpus, report, manifest["root_bindings"], "test", manifest)
    assert ordinary["summary"]["eligible_cases"] == 0
    assert ordinary["summary"]["detection"]["fp"] == 0


def test_reference_mixed_coverage_has_partial_status_and_exact_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    bundle, corpus = bundle_pair(
        tmp_path,
        monkeypatch,
        "Здесь ашибка.",
        "Здесь ошибка.",
        extra_pairs=(("Другой пример работает.", "Другой пример работает."),),
    )
    report, manifest = capture_pair(tmp_path, corpus, json_server, {"ашибка": ["ошибка"]})
    report["documents"][0]["extraction_status"] = "partial"
    manifest["report_sha256"] = digest(report)
    result = reference_score(corpus, report, bundle, "test", manifest)
    summary = result["scopes"]["pipeline"]["summary"]
    assert summary["pairs"] == 2
    assert summary["evaluated"] == summary["denominator"] == summary["exact_matches"] == 1
    assert summary["undetermined"] == 1 and summary["rejected"] == 0
    assert summary["status"] == "PARTIAL" and summary["exact_match_rate"] == 1.0
