from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from conftest import JsonServer
from test_check import (
    _config,
    _findings,
    _languagetool_config,
    _llm_finding,
    _run,
    _vale_engine_factory,
    languagetool_responder,
    llama_responder,
)

from language_review.check import build_units, check_paths, map_finding, reconcile
from language_review.check_models import Candidate, RawFinding
from language_review.config import CheckSettings, load_config
from language_review.extraction import extract_paths
from language_review.review_report import review_document, review_human
from language_review.review_verify import verify_review

ROOT = Path(__file__).parents[1]
SCHEMA = json.loads((ROOT / "docs/review-report.schema.json").read_text(encoding="utf-8"))


def _candidates(tmp_path: Path, text: str, specs: list[tuple[Any, ...]]) -> list[Candidate]:
    source = tmp_path / "sample.txt"
    source.write_bytes(text.encode())
    extracted = extract_paths([str(source)], load_config())
    [unit] = build_units(extracted.documents, [CheckSettings()])
    candidates = []
    for engine, start, end, replacements, rule, category in specs:
        raw = RawFinding(
            engine,
            unit.index,
            "plain",
            start,
            end,
            category,
            rule,
            None,
            f"Гипотеза {rule}",
            replacements,
            "model" if engine == "llm" else "rule",
        )
        mapped = map_finding(raw, unit)
        assert isinstance(mapped, Candidate)
        candidates.append(mapped)
    return candidates


def test_reconcile_shared_edit_and_transitive_alternatives(tmp_path: Path) -> None:
    candidates = _candidates(
        tmp_path,
        "Это ошыбка.",
        [
            ("languagetool", 4, 10, ("ошибка",), "TYPO", "spelling"),
            ("llm", 4, 10, ("ошибка", "ошибки"), "PROMPT", "grammar"),
            ("vale", 4, 10, ("ошибки",), "Other", "grammar"),
        ],
    )
    findings = reconcile(0, candidates)
    assert sorted(len(item.candidates) for item in findings) == [1, 2]
    merged = next(item for item in findings if len(item.candidates) == 2)
    assert {source.raw.engine for source in merged.candidates} == {"languagetool", "llm"}
    assert {suggestion.engine_replacement for suggestion in merged.suggestions} == {
        "ошибка",
        "ошибки",
    }
    # Попарная проверка исключает A-B-C даже при общей правке у каждой соседней пары.
    assert reconcile(0, list(reversed(candidates))) == findings


@pytest.mark.parametrize("common", [False, True], ids=["triangle", "global_intersection"])
def test_reconcile_requires_one_edit_shared_by_entire_group(tmp_path: Path, common: bool) -> None:
    replacements = [("рот", "мот"), ("мот", "бот"), ("рот", "бот")]
    if common:
        replacements = [(*values, "кит") for values in replacements]
    candidates = _candidates(
        tmp_path,
        "кот",
        [
            (engine, 0, 3, values, "RULE", "grammar")
            for engine, values in zip(("languagetool", "vale", "llm"), replacements, strict=True)
        ],
    )
    expected = reconcile(0, candidates)
    assert sorted(len(item.candidates) for item in expected) == ([3] if common else [1, 2])
    assert sum(len(item.candidates) for item in expected) == 3
    result = check_paths(
        [str(tmp_path / "sample.txt")],
        load_config(),
        check_overrides={"engines": ("vale",)},
        engine_factory=_vale_engine_factory("{}"),
    )
    result.documents[0].findings = expected
    report = review_document(result)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    for item in report["items"]:
        assert item["agreement_basis"] == (
            "shared_minimal_edit" if len(item["evidence"]) > 1 else "single_evidence"
        )
        assert item["agreement"] == sorted({value["engine"] for value in item["evidence"]})
        assert item["conflicting_suggestions"] is True
    for order in permutations(candidates):
        findings = reconcile(0, list(order))
        assert findings == expected
        for finding in findings:
            edits = [
                {suggestion.edit for suggestion in item.suggestions if suggestion.edit is not None}
                for item in finding.candidates
            ]
            assert set.intersection(*edits)

def test_reconcile_same_edit_text_at_different_ranges_is_independent(tmp_path: Path) -> None:
    candidates = _candidates(
        tmp_path,
        "кот кот",
        [("vale", 0, 3, ("рот",), "RULE", "grammar"), ("llm", 4, 7, ("рот",), "RULE", "grammar")],
    )
    [first], [second] = (item.suggestions for item in candidates)
    assert first.edit is not None and second.edit is not None
    assert first.edit[2] == second.edit[2]
    assert first.edit[:2] != second.edit[:2]
    assert len(reconcile(0, candidates)) == 2

@pytest.mark.parametrize("layout", ["units", "ranges", "dense", "rules"])
def test_reconcile_independent_findings_avoid_global_pair_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    import language_review.check as module

    count = 300
    candidates = _candidates(
        tmp_path,
        " ".join(["кот"] * count) if layout == "ranges" else "кот",
        [
            (
                "vale",
                index * 4 if layout == "ranges" else 0,
                index * 4 + 3 if layout == "ranges" else 3,
                (f"слово{index}" if layout == "dense" else "рот",),
                f"RULE{index}" if layout == "rules" else "RULE",
                "grammar",
            )
            for index in range(count)
        ],
    )
    if layout == "units":
        candidates = [
            replace(item, unit=replace(item.unit, index=index), raw=replace(item.raw, unit=index))
            for index, item in enumerate(candidates)
        ]
    comparisons = 0
    original = module._same_defect

    def counted(first: Candidate, second: Candidate) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(first, second)

    monkeypatch.setattr(module, "_same_defect", counted)
    findings = reconcile(0, candidates)
    assert len(findings) == count
    assert comparisons <= count * 2
    assert reconcile(0, list(reversed(candidates))) == findings


def test_overlaps_different_rules_and_repeated_quotes_remain_atomic(tmp_path: Path) -> None:
    candidates = _candidates(
        tmp_path,
        "ошыбка ошыбка ошыбка",
        [
            ("languagetool", 0, 6, ("ошибка",), "TYPO", "spelling"),
            ("llm", 0, 13, ("неточность",), "PROMPT", "grammar"),
            ("vale", 7, 20, (), "Grammar", "grammar"),
            ("languagetool", 7, 13, ("ошибка",), "TYPO", "spelling"),
            ("languagetool", 0, 6, ("ошибка",), "DIFFERENT_DIAGNOSIS", "spelling"),
        ],
    )
    findings = reconcile(0, candidates + candidates[:1])
    assert len(findings) == 5
    assert len({item.id for item in findings}) == 5
    assert sorted((item.source_start, item.source_end) for item in findings) == [
        (0, 6),
        (0, 6),
        (0, 13),
        (7, 13),
        (7, 20),
    ]


def _review_run(
    tmp_path: Path, server: JsonServer, appdata: Path, text: bytes, *extra: str
) -> tuple[dict[str, Any], Any, Path, Path]:
    source = tmp_path / "sample.cs"
    source.write_bytes(text)
    config = _languagetool_config(tmp_path, server.url)
    process = _run(
        [
            "--config",
            str(config),
            "check",
            str(source),
            "--view",
            "review",
            "--format",
            "json",
            *extra,
        ],
        appdata,
    )
    report = json.loads(process.stdout)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    assert source.read_bytes() == text
    return report, process, source, config


def test_review_cli_exact_unicode_context_evidence_and_stale_source(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    original = (
        "\ufeff// 😀 Это ошыбка.\r\n\r\n// This is an English comment.\r\n\r\n"
        'class Example { void Run() { Console.WriteLine("Здесь ошыбка."); } }\r\n'
    ).encode()
    report, process, source, _ = _review_run(tmp_path, json_server, isolated_appdata, original)
    assert process.returncode == 1
    assert len(report["items"]) == 2
    assert {item["context"][0]["kind"] for item in report["items"]} == {
        "comment",
        "user_facing_string",
    }
    for item in report["items"]:
        assert item["source_sha256"] == hashlib.sha256(original).hexdigest()
        assert item["priority"] == "inspect_first"
        assert item["decision"] == "agent_review_required"
        start, end = item["range"]["start"], item["range"]["end"]
        assert original[start["byte_offset"] : end["byte_offset"]].decode() == "ошыбка"
        assert item["evidence"][0]["rule_id"] == "MORFOLOGIK_RULE_RU_RU"
        assert item["evidence"][0]["checked_excerpt"] == "ошыбка"
    assert report["items"][0]["range"]["start"]["utf16_column"] == (
        report["items"][0]["range"]["start"]["column"] + 1
    )
    fragments = report["analysis"]["documents"][0]["fragments"]
    assert any(
        fragment["checks"]["languagetool"]["reason"] == "no_russian_text" for fragment in fragments
    )
    saved = tmp_path / "review.json"
    saved.write_text(json.dumps(report), encoding="utf-8")
    shown = _run(["show-review", str(saved), "--all"], isolated_appdata)
    assert shown.returncode == 0 and "Показано 2/2" in shown.stdout
    command = ["verify-review", str(saved), "--root", f"root-0001={source}", "--format", "json"]
    verified = _run(command, isolated_appdata)
    assert verified.returncode == 0
    assert json.loads(verified.stdout)["status"] == "current"
    human = _run(
        ["verify-review", str(saved), "--root", f"root-0001={source}"],
        isolated_appdata,
    )
    assert human.returncode == 0
    assert human.stdout.startswith("Сверка ревью: current\n")
    assert f"{json.dumps(report['items'][0]['document_id'])}: current" in human.stdout
    assert not human.stdout.lstrip().startswith("{")
    tampered = json.loads(json.dumps(report))
    [suggestion] = tampered["items"][0]["suggestions"]
    assert suggestion["edit"] is not None
    suggestion["edit"]["original"] = "подлог"
    saved.write_text(json.dumps(tampered), encoding="utf-8")
    stale_suggestion = _run(command, isolated_appdata)
    assert stale_suggestion.returncode == 4
    assert json.loads(stale_suggestion.stdout)["documents"][0]["reason"] == "quote_mismatch"
    malformed = json.loads(json.dumps(report))
    malformed["items"][0]["range"]["start"] = [
        "byte_offset",
        "line",
        "column",
        "utf16_column",
    ]
    saved.write_text(json.dumps(malformed), encoding="utf-8")
    invalid_coordinate = _run(command, isolated_appdata)
    assert invalid_coordinate.returncode == 2
    assert "Traceback" not in invalid_coordinate.stderr
    assert invalid_coordinate.stdout == ""
    missing_field = json.loads(json.dumps(report))
    del missing_field["items"][0]["context"]
    saved.write_text(json.dumps(missing_field), encoding="utf-8")
    invalid_report = _run(command, isolated_appdata)
    assert invalid_report.returncode == 2
    assert "Traceback" not in invalid_report.stderr
    assert invalid_report.stdout == ""
    # Тот же хеш с ошибочной цитатой не даёт current.
    report["items"][0]["excerpt"] = "ошибка"
    saved.write_text(json.dumps(report), encoding="utf-8")
    assert verify_review(saved, {"root-0001": source})["documents"][0] == {
        "document_id": report["items"][0]["document_id"],
        "status": "stale",
        "reason": "quote_mismatch",
    }
    source.write_bytes(original + b" ")
    stale = _run(command, isolated_appdata)
    assert stale.returncode == 4
    assert json.loads(stale.stdout)["documents"][0]["reason"] == "source_sha256_mismatch"


def test_review_preserves_conflicts_protected_risks_and_all_evidence(tmp_path: Path) -> None:
    text = "Если это не ошыбка, вызовите id.ToString() и {0}."
    candidates = _candidates(
        tmp_path,
        text,
        [
            ("languagetool", 12, 18, ("ошибка", "ошибки"), "TYPO", "spelling"),
            ("llm", 12, 18, ("неточность",), "PROMPT", "style"),
        ],
    )
    source = tmp_path / "sample.txt"
    config = load_config(_config(tmp_path, '[check]\nengines=["vale"]\n'))
    result = check_paths([str(source)], config, engine_factory=_vale_engine_factory("{}"))
    result.documents[0].findings = reconcile(0, candidates)
    report = review_document(result)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    assert len(report["items"]) == 2
    assert all(item["priority"] == "needs_context" for item in report["items"])
    assert all("competing_diagnoses" in item["risks"] for item in report["items"])
    assert all("conditional_or_negated_context" in item["risks"] for item in report["items"])
    assert report["items"][0]["related_findings"] == [report["items"][1]["id"]]
    assert all("id.ToString" not in item["context"][0]["quote"] for item in report["items"])
    deterministic = next(item for item in report["items"] if item["agreement"] == ["languagetool"])
    assert deterministic["evidence"][0]["replacements"] == ["ошибка", "ошибки"]


@pytest.mark.parametrize(
    "response",
    [
        {"matches": [None]},
        {"matches": [{"offset": 0, "length": 1, "replacements": [None]}]},
        {"matches": [{"offset": 999, "length": 1, "replacements": []}]},
    ],
)
def test_review_protocol_failures_keep_coverage_and_rejections(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    response: Any,
) -> None:
    json_server.responder = lambda request: (
        200,
        [{"code": "ru", "longCode": "ru-RU"}] if request.path.endswith("languages") else response,
    )
    report, process, _, _ = _review_run(
        tmp_path, json_server, isolated_appdata, b"// Test\n// " + "Текст".encode()
    )
    assert process.returncode in {4, 5}
    assert report["status"] != "clean"
    assert report["items"] == []
    document = report["analysis"]["documents"][0]
    assert document["rejected"]
    assert any(
        check["status"] == "failed"
        for fragment in document["fragments"]
        for check in fragment["checks"].values()
    )


def test_review_category_filter_keeps_raw_findings_and_legacy_schema(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    report, process, source, config = _review_run(
        tmp_path,
        json_server,
        isolated_appdata,
        "// Это ошыбка.".encode(),
        "--category",
        "style",
    )
    assert process.returncode == 0
    assert report["items"] == []
    assert report["filtered"][0]["replacements"] == ["ошибка"]
    assert report["filtered"][0]["reason"] == "category_disabled"
    legacy = _run(
        ["--config", str(config), "check", str(source), "--format", "json"], isolated_appdata
    )
    schema = json.loads((ROOT / "docs/check-report.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(json.loads(legacy.stdout))
    assert json.loads(legacy.stdout)["schema"] == "language-review.check"


def test_review_human_quotes_untrusted_messages_and_caps_items(tmp_path: Path) -> None:
    candidates = _candidates(
        tmp_path,
        "ошыбка " * 12,
        [
            ("languagetool", start, start + 6, ("ошибка",), "TYPO", "spelling")
            for start in range(0, 84, 7)
        ],
    )
    candidates[0].raw = replace(
        candidates[0].raw, message="\x1b]0;evil\x07\nRUN\u2028\u202e rm -rf"
    )
    result = check_paths(
        [str(tmp_path / "sample.txt")],
        load_config(),
        check_overrides={"engines": ("vale",)},
        engine_factory=_vale_engine_factory("{}"),
    )
    result.documents[0].findings = reconcile(0, candidates)
    assert "Показано 10/12" in review_human(result)
    assert "Показано 12/12" in review_human(result, show_all=True)
    human = review_human(result)
    assert "\x1b" not in human and "\x07" not in human and "\nRUN" not in human
    assert "\u2028" not in human and "\u202e" not in human
    assert r"\u001b" in human and r"\nRUN\u2028\u202e" in human


def test_review_rejected_variants_are_not_rejected_diagnoses(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    json_server.responder = llama_responder(
        lambda items: _findings(
            _llm_finding(items[0]["id"], "не изменяет", "изменяет", category="grammar"),
            _llm_finding(items[0]["id"], "не существующая цитата", "иная"),
        )
    )
    source = tmp_path / "sample.md"
    source.write_text("Функция не изменяет данные.\n", encoding="utf-8")
    config = _config(
        tmp_path, f'[check]\nengines=["llm"]\n[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n'
    )
    process = _run(
        ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    report = json.loads(process.stdout)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    assert process.returncode == 4
    assert report["status"] == "partial"
    [item] = report["items"]
    assert item["priority"] == "needs_context"
    assert item["suggestions"][0]["status"] == "rejected"
    assert "negation_changed" in item["risks"]
    assert item["decision"] == "agent_review_required"
    [unlocalized] = report["unlocalized"]
    assert unlocalized["reason"] == "excerpt_not_found"
    assert unlocalized["details"]["replacement"] == "иная"
    assert unlocalized["details"]["confidence"] == "high"
    assert "range" not in unlocalized


def test_review_preserves_protected_rejection_without_source_coordinates(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    json_server.responder = llama_responder(
        lambda items: _findings(
            _llm_finding(items[0]["id"], "⟦1⟧", "id. ToString()"),
            _llm_finding(items[0]["id"], "файлы ⟦1⟧", "файлы 1"),
        )
    )
    source = tmp_path / "doc.md"
    source.write_text("Функция не изменяет файлы {path}.\n", encoding="utf-8")
    original = source.read_bytes()
    config = _config(
        tmp_path, f'[check]\nengines=["llm"]\n[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n'
    )
    process = _run(
        ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    report = json.loads(process.stdout)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    [rejected] = report["unlocalized"]
    assert rejected["reason"] == "targets_protected_text"
    assert rejected["raw"]["replacements"] == ["id. ToString()"]
    assert "range" not in rejected
    [item] = report["items"]
    assert "replacement_overlaps_protected_range" in item["risks"]
    assert "protected_content" in item["risks"]
    assert item["priority"] == "needs_context"
    assert source.read_bytes() == original


@pytest.mark.parametrize("invalid_llm", ["not json", '{"findings":[{}]}'])
def test_review_invalid_llm_is_unavailable_not_clean(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    invalid_llm: str,
) -> None:
    json_server.responder = llama_responder(invalid_llm)
    source = tmp_path / "doc.md"
    source.write_text("Это корректный текст.\n", encoding="utf-8")
    config = _config(
        tmp_path, f'[check]\nengines=["llm"]\n[check.llm.llama_cpp]\nendpoint="{json_server.url}"\n'
    )
    process = _run(
        ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    report = json.loads(process.stdout)
    assert process.returncode == 5
    assert report["status"] == "unavailable"
    assert report["analysis"]["documents"][0]["fragments"][0]["checks"]["llm"]["status"] == "failed"


def test_review_partial_extraction_and_evidence_preserve_incompleteness(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    source = tmp_path / "header.h"
    original = "// Здесь ошыбка.\nint run(void);\n".encode()
    source.write_bytes(original)
    config = _languagetool_config(tmp_path, json_server.url)
    process = _run(
        ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    report = json.loads(process.stdout)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    assert process.returncode == 4
    assert report["status"] == "partial"
    assert report["items"][0]["coverage"]["extraction_status"] == "partial"
    assert "incomplete_coverage" in report["items"][0]["risks"]
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "line,span",
    [
        (True, [1, 3]),
        (1, [0, 3]),
        (1, [-2, 3]),
        (1, [4, 2]),
        (1, [1, 999]),
        (2, [-3, 2]),
        (99, [1, 2]),
    ],
)
def test_vale_numeric_coordinates_must_fit_the_actual_line(
    tmp_path: Path,
    line: int,
    span: list[int],
) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("ошыбка\nошыбка здесь.\n", encoding="utf-8")
    alert = {"Check": "X.Rule", "Line": line, "Span": span, "Message": "Гипотеза"}
    result = check_paths(
        [str(source)],
        load_config(),
        check_overrides={"engines": ("vale",)},
        engine_factory=_vale_engine_factory(json.dumps({"stdin.txt": [alert]})),
    )
    assert result.status != "clean"
    assert result.engines[0].diagnostics == ["vale_schema_violation"]
    assert not review_document(result)["items"]


@pytest.mark.parametrize(
    "path", ["../escape.txt", "C:/private/a.txt", "/tmp/a.txt", "a:stream", "a\\b.txt"]
)
def test_verify_review_refuses_report_controlled_paths(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    path: str,
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    report, _, _, _ = _review_run(tmp_path, json_server, isolated_appdata, "// ошыбка".encode())
    report["analysis"]["roots"][0]["kind"] = "directory"
    report["analysis"]["documents"][0]["path"] = path
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report), encoding="utf-8")
    verified = verify_review(saved, {"root-0001": tmp_path})
    assert verified["status"] == "unverified"
    assert verified["documents"][0]["reason"] == "unsafe_relative_path"


def test_verify_review_reports_missing_source_as_unavailable(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    report, _, source, _ = _review_run(
        tmp_path, json_server, isolated_appdata, "// ошыбка".encode()
    )
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report), encoding="utf-8")
    source.unlink()
    verified = verify_review(saved, {"root-0001": source})
    assert verified["documents"][0] == {
        "document_id": report["items"][0]["document_id"],
        "status": "unverified",
        "reason": "source_unavailable",
    }


@pytest.mark.skipif(shutil.which("vale") is None, reason="Vale недоступен")
def test_review_real_vale_cli_deterministic_example_and_unavailable(
    tmp_path: Path,
    isolated_appdata: Path,
) -> None:
    source = tmp_path / "sample.md"
    original = "Это это пример.\n".encode()
    source.write_bytes(original)
    config = _config(tmp_path, '[check]\nengines=["vale"]\n')
    args = ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"]
    first, second = _run(args, isolated_appdata), _run(args, isolated_appdata)
    report, repeated = json.loads(first.stdout), json.loads(second.stdout)
    assert first.returncode == second.returncode == 1
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    for value in (report, repeated):
        value["analysis"]["engines"][0].pop("elapsed_ms")
    assert report == repeated
    assert source.read_bytes() == original
    assert report["items"][0]["excerpt"] == "Это это"
    assert report["items"][0]["priority"] == "inspect_first"
    example = json.loads((ROOT / "docs/review-report.example.json").read_text(encoding="utf-8"))
    example["analysis"]["engines"][0].pop("elapsed_ms")
    if example["analysis"]["engines"][0]["details"] == report["analysis"]["engines"][0]["details"]:
        assert report == example
    down = _config(tmp_path, '[check]\nengines=["vale"]\n[check.vale]\nprogram="missing-vale"\n')
    process = _run(
        ["--config", str(down), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    assert process.returncode == 5
    assert json.loads(process.stdout)["status"] == "unavailable"


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", b"\xff\xfe"), ("cp1251", b"")])
def test_review_verification_checks_encoded_bytes_and_coordinates(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    encoding: str,
    bom: bytes,
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    source = tmp_path / "sample.txt"
    original = bom + "Это ошыбка.\r\n".encode(encoding)
    source.write_bytes(original)
    config = _languagetool_config(tmp_path, json_server.url)
    process = _run(
        ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    report = json.loads(process.stdout)
    saved = tmp_path / "review.json"
    saved.write_text(json.dumps(report), encoding="utf-8")
    assert verify_review(saved, {"root-0001": source})["status"] == "current"
    report["items"][0]["range"]["start"]["line"] = 99
    saved.write_text(json.dumps(report), encoding="utf-8")
    verified = verify_review(saved, {"root-0001": source})
    assert verified["status"] == "stale"
    assert verified["documents"][0]["reason"] == "coordinate_mismatch"
    assert source.read_bytes() == original


def test_review_schema_and_cli_refuse_malformed_saved_report(
    tmp_path: Path,
    isolated_appdata: Path,
) -> None:
    example = json.loads((ROOT / "docs/review-report.example.json").read_text(encoding="utf-8"))
    del example["items"][0]["priority"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(SCHEMA).validate(example)
    saved = tmp_path / "review.json"
    saved.write_text('{"schema":"unknown"}', encoding="utf-8")
    for command in ("show-review", "verify-review"):
        process = _run([command, str(saved)], isolated_appdata)
        assert process.returncode == 2
        assert "Traceback" not in process.stderr
        assert process.stdout == ""
    for field, value in (
        ("schema", "language-review.other"),
        ("schema_version", "2.0"),
    ):
        malformed = json.loads(
            (ROOT / "docs/review-report.example.json").read_text(encoding="utf-8")
        )
        malformed["analysis"][field] = value
        saved.write_text(json.dumps(malformed), encoding="utf-8")
        for command in ("show-review", "verify-review"):
            process = _run([command, str(saved)], isolated_appdata)
            assert process.returncode == 2
            assert "Traceback" not in process.stderr
            assert process.stdout == ""
