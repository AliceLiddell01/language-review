from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from conftest import JsonServer, RecordedRequest

from language_review.check import check_paths
from language_review.config import load_config
from language_review.engine_http import EngineHttpError, join_url, request_json
from language_review.engine_llm import SYSTEM_PROMPT, response_schema
from language_review.errors import ConfigurationError

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "docs" / "check-report.schema.json").read_text(encoding="utf-8")
)
VALE = shutil.which("vale")
CLOSED_PORT_URL = "http://127.0.0.1:9"


def _validate(report: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(report)


def _run(args: list[str], appdata: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["APPDATA"] = str(appdata)
    environment["XDG_CONFIG_HOME"] = str(appdata)
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "language_review", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _languagetool_config(tmp_path: Path, url: str) -> Path:
    return _config(
        tmp_path,
        f'[check]\nengines = ["languagetool"]\n[check.languagetool]\nendpoint = "{url}"\n',
    )


def _utf16(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def languagetool_responder(misspellings: dict[str, list[str]]) -> Any:
    """Протокол LanguageTool: смещения в UTF-16, как у настоящего сервера."""

    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/v2/languages":
            return 200, [{"name": "Russian", "code": "ru", "longCode": "ru-RU"}]
        assert request.path == "/v2/check"
        text = request.body["text"]
        matches = []
        for word, replacements in misspellings.items():
            start = text.find(word)
            if start >= 0:
                matches.append(
                    {
                        "message": "Возможно найдена орфографическая ошибка.",
                        "offset": _utf16(text, start),
                        "length": _utf16(text, start + len(word)) - _utf16(text, start),
                        "replacements": [{"value": value} for value in replacements],
                        "rule": {
                            "id": "MORFOLOGIK_RULE_RU_RU",
                            "issueType": "misspelling",
                            "category": {"id": "TYPOS"},
                        },
                    }
                )
        if "X" in text:
            # Правило, которое касается только заглушки, должно быть отбраковано.
            start = text.index("X")
            matches.append(
                {
                    "message": "Слово на латинице.",
                    "offset": _utf16(text, start),
                    "length": 1,
                    "replacements": [],
                    "rule": {"id": "LATIN", "category": {"id": "MISC"}},
                }
            )
        return 200, {"software": {"version": "stub"}, "matches": matches}

    return respond


def llama_responder(content: Any, *, finish_reason: str = "stop") -> Any:
    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/health":
            return 200, {"status": "ok"}
        if request.path == "/v1/models":
            return 200, {"data": [{"id": "stub-model", "aliases": ["stub-alias"]}]}
        if request.path == "/props":
            return 200, {"build_info": "b0-stub", "model_path": "C:/private/model.gguf"}
        assert request.path == "/v1/chat/completions"
        items = json.loads(request.body["messages"][1]["content"])["items"]
        value = content(items) if callable(content) else content
        return 200, {
            "choices": [{"message": {"content": value}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    return respond


def _findings(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)}, ensure_ascii=False)


def _llm_finding(item: str, excerpt: str, replacement: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "item": item,
        "category": extra.get("category", "spelling"),
        "excerpt": excerpt,
        "replacement": replacement,
        "explanation": extra.get("explanation", "Опечатка."),
        "confidence": extra.get("confidence", "high"),
    }


def _report(
    path: Path, config: Path, appdata: Path, *extra: str
) -> tuple[int, dict[str, Any], str]:
    process = _run(
        ["--config", str(config), "check", str(path), "--format", "json", *extra], appdata
    )
    report = json.loads(process.stdout)
    _validate(report)
    return process.returncode, report, process.stderr


def test_languagetool_pipeline_maps_utf16_offsets_and_rejects_placeholder_matches(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = languagetool_responder({"праверить": ["проверить", "проверять"]})
    source = tmp_path / "main.rs"
    original = "fn main() {\n    // 😀 Вызовите id.ToString() и праверить {0}.\n}\n".encode()
    source.write_bytes(original)
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 1
    assert report["status"] == "findings"
    document = report["documents"][0]
    [finding] = document["findings"]
    assert finding["excerpt"] == "праверить"
    start = finding["range"]["start"]
    line = original.decode().splitlines()[1]
    assert start["line"] == 2
    assert start["column"] == line.index("праверить") + 1
    assert start["utf16_column"] == _utf16(line, line.index("праверить")) + 1
    assert original[start["byte_offset"] : finding["range"]["end"]["byte_offset"]].decode() == (
        "праверить"
    )
    assert [item["edit"]["text"] for item in finding["suggestions"]] == ["проверить", "проверять"]
    assert {item["status"] for item in finding["suggestions"]} == {"candidate"}
    assert {item["origin"] for item in finding["suggestions"]} == {"dictionary"}
    assert [item["reason"] for item in document["rejected"]] == ["targets_protected_text"]
    # Движок получил текст без кода: технический токен и форматное поле заменены заглушкой.
    sent = json_server.requests[-1].body["text"]
    assert "ToString" not in sent and "{0}" not in sent
    assert source.read_bytes() == original
    # Отчёт можно передавать: в нём нет локальных путей к входам и конфигурации.
    assert str(tmp_path) not in json.dumps(report, ensure_ascii=False)
    assert report["configuration"]["sources"]["engines"] == "файл конфигурации"


def test_reconciliation_merges_overlapping_engines_and_keeps_sources(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    lt = languagetool_responder({"ошыбок": ["ошибок"], "фаил": ["файл"]})

    def llm(items: list[dict[str, Any]]) -> str:
        return _findings(
            _llm_finding(items[0]["id"], "без ошыбок", "без ошибок", category="grammar"),
            _llm_finding(items[0]["id"], "Сохраните", None, category="style"),
        )

    llama = llama_responder(llm)

    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path.startswith("/v2/"):
            return lt(request)
        return llama(request)

    json_server.responder = respond
    source = tmp_path / "doc.md"
    source.write_text("Сохраните фаил без ошыбок.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["languagetool", "llm"]
[check.languagetool]
endpoint = "{json_server.url}"
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
""",
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 1
    findings = {item["excerpt"]: item for item in report["documents"][0]["findings"]}
    assert set(findings) == {"Сохраните", "фаил", "без ошыбок"}
    merged = findings["без ошыбок"]
    assert merged["agreement"] == ["languagetool", "llm"]
    # Совместимые категории (опечатка у словаря, грамматика у модели) — одна находка.
    assert (merged["category"], merged["categories"]) == ("spelling", ["grammar", "spelling"])
    assert {(item["engine"], item["confidence"]) for item in merged["sources"]} == {
        ("languagetool", None),
        ("llm", "high"),
    }
    # Одинаковая правка двух движков — одна кандидатура с обоими источниками.
    candidates = [item for item in merged["suggestions"] if item["status"] == "candidate"]
    assert [(item["edit"]["text"], item["engines"]) for item in candidates] == [
        ("ошибок", ["languagetool", "llm"])
    ]
    assert merged["conflicting_suggestions"] is False
    # Соседние находки одной строки без пересечения не склеиваются.
    assert findings["фаил"]["agreement"] == ["languagetool"]
    assert findings["Сохраните"]["agreement"] == ["llm"]
    llm_run = next(item for item in report["engines"] if item["name"] == "llm")
    assert llm_run["details"]["model"] == "stub-model"
    assert "model_path" not in json.dumps(report)


def test_llm_rejects_invalid_quotes_placeholders_and_meaning_changes(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    def llm(items: list[dict[str, Any]]) -> str:
        item = items[0]["id"]
        return _findings(
            _llm_finding(item, "несуществующая цитата", "текст"),
            _llm_finding("t99", "Функция", None),
            _llm_finding(item, "⟦1⟧", "1"),
            _llm_finding(item, "не изменяет", "изменяет", category="style"),
            _llm_finding(item, "файлы ⟦1⟧", "файлы 1"),
            _llm_finding(item, "Функция", "Функция"),
            _llm_finding(item, "сетю", "сетью"),
            _llm_finding(item, "сетю", "сетью"),
        )

    json_server.responder = llama_responder(llm)
    source = tmp_path / "notes.txt"
    source.write_text(
        "Функция не изменяет файлы {path} и не обращается к сетю.\n", encoding="utf-8"
    )
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 1
    document = report["documents"][0]
    reasons = sorted(item["reason"] for item in document["rejected"])
    assert reasons == [
        "duplicate_finding",
        "excerpt_not_found",
        "replacement_identical",
        "targets_protected_text",
        "unknown_item",
    ]
    findings = {item["excerpt"]: item for item in document["findings"]}
    negation = findings["не изменяет"]["suggestions"][0]
    assert negation["status"] == "rejected"
    assert "negation_changed" in negation["reasons"]
    placeholder = findings["файлы {path}"]["suggestions"][0]
    assert placeholder["status"] == "rejected"
    assert placeholder["reasons"] == ["replacement_overlaps_protected_range"]
    assert findings["сетю"]["suggestions"][0]["status"] == "candidate"
    assert findings["сетю"]["sources"][0]["engine_excerpt"] == "сетю"


@pytest.mark.parametrize(
    ("content", "finish_reason", "reason"),
    [
        ('{"findings": [', "stop", "llm_invalid_json"),
        ('{"findings": []}', "length", "llm_truncated_response"),
        ('{"result": []}', "stop", "llm_schema_violation"),
    ],
)
def test_llm_response_failures_are_not_clean_results(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    content: str,
    finish_reason: str,
    reason: str,
) -> None:
    json_server.responder = llama_responder(content, finish_reason=finish_reason)
    source = tmp_path / "notes.txt"
    source.write_text("Корректный текст.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, stderr = _report(source, config, isolated_appdata)

    assert code == 5
    assert report["status"] == "unavailable"
    run = report["engines"][0]
    assert run["status"] == "error"
    assert reason in run["diagnostics"]
    assert run["statistics"]["units_failed"] == 1
    assert report["documents"][0]["status"] == "unavailable"
    assert "Движок llm: error" in stderr


def test_clean_text_and_both_llm_providers_share_prompt_and_schema(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/api/version":
            return 200, {"version": "0.0-stub"}
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "gemma4:e4b", "model": "gemma4:e4b"}]}
        if request.path == "/api/chat":
            return 200, {"message": {"content": '{"findings": []}'}, "done_reason": "stop"}
        return llama_responder('{"findings": []}')(request)

    json_server.responder = respond
    source = tmp_path / "notes.txt"
    source.write_text("Программа сохраняет настройки в файл конфигурации.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["llm"]
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
[check.llm.ollama]
endpoint = "{json_server.url}"
""",
    )

    llama_code, llama_report, _ = _report(source, config, isolated_appdata)
    ollama_code, ollama_report, _ = _report(
        source, config, isolated_appdata, "--llm-provider", "ollama"
    )

    assert (llama_code, llama_report["status"]) == (0, "clean")
    assert (ollama_code, ollama_report["status"]) == (0, "clean")
    assert ollama_report["configuration"]["sources"]["llm.provider"] == "аргумент командной строки"
    llama_request = next(
        item for item in json_server.requests if item.path == "/v1/chat/completions"
    )
    ollama_request = next(item for item in json_server.requests if item.path == "/api/chat")
    assert llama_request.body["messages"] == ollama_request.body["messages"]
    assert llama_request.body["messages"][0]["content"] == SYSTEM_PROMPT
    schema = llama_request.body["response_format"]["json_schema"]["schema"]
    assert schema == ollama_request.body["format"] == response_schema(4)
    assert llama_request.body["chat_template_kwargs"] == {"enable_thinking": False}
    assert ollama_request.body["think"] is False
    assert llama_request.body["temperature"] == ollama_request.body["options"]["temperature"] == 0


def test_unavailable_engine_is_not_reported_as_success(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Текст для проверки.\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, CLOSED_PORT_URL)

    code, report, stderr = _report(source, config, isolated_appdata)

    assert code == 5
    assert report["status"] == "unavailable"
    assert report["engines"][0]["status"] == "unavailable"
    # Закрытый порт в Windows отвечает отказом соединения, в WSL2 с mirrored-сетью — тайм-аутом.
    diagnostics = report["engines"][0]["diagnostics"]
    assert "engine_unavailable" in diagnostics
    assert len(diagnostics) == 2 and {"connection_failed", "timeout"} & set(diagnostics)
    assert report["documents"][0]["fragments"][0]["checks"]["languagetool"]["status"] == "failed"
    assert "Движок languagetool: unavailable" in stderr


def test_missing_model_and_selection_are_explicit(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = llama_responder('{"findings": []}')
    config = _config(
        tmp_path,
        f"""[check]
engines = ["llm"]
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
model = "other-model"
""",
    )
    result = check_paths([str(tmp_path / "config.toml")], load_config(config))

    assert result.status == "unavailable"
    assert result.engines[0].diagnostics[0] == "model_not_found"
    assert not any(item.path == "/v1/chat/completions" for item in json_server.requests)


def test_partial_extraction_keeps_findings_but_reports_incompleteness(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    source = tmp_path / "header.h"
    source.write_text("// Здесь ошыбка в комментарии.\nint run(void);\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, stderr = _report(source, config, isolated_appdata)

    assert code == 4
    assert report["status"] == "partial"
    document = report["documents"][0]
    assert document["extraction_status"] == "partial"
    assert document["status"] == "partial"
    assert [item["excerpt"] for item in document["findings"]] == ["ошыбка"]
    assert "Неполная проверка" in stderr


def test_category_filter_and_non_russian_fragments(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    source = tmp_path / "notes.txt"
    source.write_text("Here is English only.\n\nЗдесь ошыбка.\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, _stderr = _report(source, config, isolated_appdata, "--category", "grammar")

    assert code == 0
    assert report["status"] == "clean"
    assert report["engines"][0]["statistics"]["filtered"] == 1
    assert report["summary"]["units_without_russian_text"] == 1
    checks = [item["checks"]["languagetool"] for item in report["documents"][0]["fragments"]]
    assert checks == [
        {"status": "skipped", "reason": "no_russian_text"},
        {"status": "checked", "reason": None},
    ]
    sent = [item.body["text"] for item in json_server.requests if item.path == "/v2/check"]
    assert all("English" not in text for text in sent)


def test_remote_endpoints_require_explicit_opt_in(tmp_path: Path, isolated_appdata: Path) -> None:
    remote = '[check.languagetool]\nendpoint = "https://languagetool.example.test"\n'
    with pytest.raises(ConfigurationError, match="allow_remote_endpoints"):
        load_config(_config(tmp_path, remote))

    process = _run(
        ["--config", str(tmp_path / "config.toml"), "check", str(tmp_path)], isolated_appdata
    )
    assert process.returncode == 2
    assert "allow_remote_endpoints" in process.stderr

    allowed = load_config(_config(tmp_path, "[check]\nallow_remote_endpoints = true\n" + remote))
    settings, sources = allowed.check_settings_for_profile(None)
    assert settings.languagetool_endpoint == "https://languagetool.example.test"
    assert sources["allow_remote_endpoints"].endswith(str(tmp_path / "config.toml"))


def test_check_report_output_cannot_overwrite_inputs(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "notes.txt"
    original = "Текст.\n".encode()
    source.write_bytes(original)
    config = _languagetool_config(tmp_path, CLOSED_PORT_URL)

    process = _run(
        ["--config", str(config), "check", str(source), "--output", str(source)], isolated_appdata
    )

    assert process.returncode == 3
    assert source.read_bytes() == original


@pytest.mark.skipif(VALE is None, reason="Vale не установлен")
def test_vale_bundled_rules_find_positive_examples_and_keep_clean_text(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    positive = tmp_path / "positive.md"
    positive.write_text(
        "Юзер кликнул по кнопке, а апдейтов нет.\n\n"
        "Настройка осуществляется посредством файла.\n\n"
        "Это в в файле - пример.\n",
        encoding="utf-8",
    )
    clean = tmp_path / "clean.md"
    clean.write_text(
        "Пользователь нажимает кнопку «Сохранить», и программа записывает настройки в файл.\n\n"
        "Значение по умолчанию — пустая строка. Команда `git status` не изменяет файлы.\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, '[check]\nengines = ["vale"]\nterms = { "фреймворк" = "каркас" }\n')

    code, report, _ = _report(positive, config, isolated_appdata)
    clean_code, clean_report, _ = _report(clean, config, isolated_appdata)

    assert code == 1
    rules = {
        (source["rule_id"], finding["excerpt"])
        for finding in report["documents"][0]["findings"]
        for source in finding["sources"]
    }
    assert rules == {
        ("LanguageReview.Terminology", "Юзер"),
        ("LanguageReview.Terminology", "кликнул"),
        ("LanguageReview.Terminology", "апдейтов"),
        ("LanguageReview.Bureaucratese", "осуществляется"),
        ("LanguageReview.Bureaucratese", "посредством"),
        ("LanguageReview.Repetition", "в в"),
        ("LanguageReview.Dash", " - "),
    }
    suggestions = {
        finding["excerpt"]: [item["edit"]["text"] for item in finding["suggestions"]]
        for finding in report["documents"][0]["findings"]
    }
    assert suggestions["Юзер"] == ["Пользователь"]
    assert suggestions["апдейтов"] == ["обновлений"]
    assert (clean_code, clean_report["status"]) == (0, "clean")


@pytest.mark.skipif(VALE is None, reason="Vale не установлен")
def test_vale_project_terms_and_invalid_custom_configuration(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Мы используем фреймворк.\n", encoding="utf-8")
    config = _config(tmp_path, '[check]\nengines = ["vale"]\nterms = { "фреймворк" = "каркас" }\n')

    code, report, _ = _report(source, config, isolated_appdata)

    assert code == 1
    [finding] = report["documents"][0]["findings"]
    assert finding["sources"][0]["rule_id"] == "LanguageReview.Terms"
    assert finding["suggestions"][0]["edit"]["text"] == "каркас"

    broken = tmp_path / "broken.ini"
    broken.write_text(
        "StylesPath = missing-styles\n[*.txt]\nBasedOnStyles = Absent\n", encoding="utf-8"
    )
    config = _config(
        tmp_path, f'[check]\nengines = ["vale"]\n[check.vale]\nconfig = "{broken.name}"\n'
    )
    code, report, _ = _report(source, config, isolated_appdata)
    assert code == 5
    assert report["engines"][0]["status"] == "unavailable"
    assert str(tmp_path) not in json.dumps(report)


def test_missing_vale_program_is_controlled(tmp_path: Path, isolated_appdata: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Текст.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        '[check]\nengines = ["vale"]\n[check.vale]\nprogram = "definitely-missing-vale-binary"\n',
    )

    code, report, _ = _report(source, config, isolated_appdata)

    assert code == 5
    assert report["engines"][0]["diagnostics"] == ["engine_unavailable", "program_not_found"]


def test_check_human_output_and_profile_engine_selection(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["ошибка"]})
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("Здесь ошыбка.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["languagetool", "llm"]
[check.languagetool]
endpoint = "{json_server.url}"
[profiles.docs]
match_paths = ["docs/**"]
[profiles.docs.check]
engines = ["languagetool"]
""",
    )

    process = _run(
        ["--config", str(config), "check", str(root), "--profile", "docs"], isolated_appdata
    )

    assert process.returncode == 1, process.stderr
    assert "Языковая проверка: findings" in process.stdout
    assert "«ошыбка»" in process.stdout
    assert "варианты (требуют ручной проверки): «ошибка»" in process.stdout
    assert "предложения не применялись к исходникам" in process.stdout


def test_llm_splits_long_text_and_refuses_oversized_sentences(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    def llm(items: list[dict[str, Any]]) -> str:
        found = [item for item in items if "ошыбкой" in item["text"]]
        return _findings(*(_llm_finding(item["id"], "ошыбкой", "ошибкой") for item in found))

    json_server.responder = llama_responder(llm)
    sentence = "Это обычное предложение для проверки разбиения текста на части. "
    long_text = sentence * 6 + "Последнее предложение с ошыбкой."
    oversized = "Слово " * 120
    source = tmp_path / "notes.txt"
    source.write_text(f"{long_text}\n\n{oversized.strip()}.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["llm"]
[check.llm]
batch_chars = 200
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
""",
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 4
    document = report["documents"][0]
    [finding] = document["findings"]
    assert finding["range"]["start"]["column"] == long_text.index("ошыбкой") + 1
    checks = [item["checks"]["llm"] for item in document["fragments"]]
    assert checks == [
        {"status": "checked", "reason": None},
        {"status": "skipped", "reason": "unit_too_large"},
    ]
    sent = [
        json.loads(item.body["messages"][1]["content"])["items"]
        for item in json_server.requests
        if item.path == "/v1/chat/completions"
    ]
    assert all(len(entry["text"]) <= 400 for items in sent for entry in items)
    assert all("Слово Слово" not in entry["text"] for items in sent for entry in items)


def test_only_skipped_units_are_partial_not_unavailable(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = llama_responder('{"findings": []}')
    source = tmp_path / "notes.txt"
    source.write_text(("Слово " * 120).strip() + ".\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["llm"]
[check.llm]
batch_chars = 200
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
""",
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 4
    assert report["status"] == "partial"
    assert report["documents"][0]["status"] == "partial"
    assert report["engines"][0]["status"] == "partial"
    assert not any(item.path == "/v1/chat/completions" for item in json_server.requests)


def test_llama_server_without_models_reports_model_not_found(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/health":
            return 200, {"status": "ok"}
        assert request.path == "/v1/models"
        return 200, {"data": []}

    json_server.responder = respond
    config = _config(
        tmp_path,
        f"""[check]
engines = ["llm"]
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
""",
    )
    result = check_paths([str(tmp_path / "config.toml")], load_config(config))

    assert result.status == "unavailable"
    assert result.engines[0].diagnostics[0] == "model_not_found"


def test_truncated_chunked_response_is_a_controlled_protocol_error() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        connection, _address = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b'Transfer-Encoding: chunked\r\n\r\n10\r\n{"a"\r\n'
            )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(EngineHttpError) as raised:
            request_json(f"http://127.0.0.1:{listener.getsockname()[1]}/", timeout=5)
    finally:
        listener.close()
        thread.join(timeout=5)

    assert raised.value.code == "protocol_error"


def test_languagetool_non_list_replacements_are_rejected_not_crashed(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/v2/languages":
            return 200, [{"name": "Russian", "code": "ru", "longCode": "ru-RU"}]
        return 200, {
            "software": {"version": "stub"},
            "matches": [
                {
                    "message": "Опечатка.",
                    "offset": 0,
                    "length": 4,
                    "replacements": None,
                    "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "category": {"id": "TYPOS"}},
                }
            ],
        }

    json_server.responder = respond
    source = tmp_path / "notes.txt"
    source.write_text("Ошыбка в слове.\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 0
    document = report["documents"][0]
    assert document["findings"] == []
    assert [item["reason"] for item in document["rejected"]] == ["schema_violation"]


def test_join_url_keeps_query_and_fragment_after_path() -> None:
    assert join_url("http://127.0.0.1:9/?token=x", "/v2/languages") == (
        "http://127.0.0.1:9/v2/languages?token=x"
    )
    assert join_url("http://127.0.0.1:9/api/", "v1/models") == "http://127.0.0.1:9/api/v1/models"


def test_check_human_shows_extraction_reason(tmp_path: Path, isolated_appdata: Path) -> None:
    source = tmp_path / "blob.txt"
    source.write_bytes(b"abc\x00\x00def\n")
    config = _config(tmp_path, '[check]\nengines = ["languagetool"]\n')

    process = _run(["--config", str(config), "check", str(source)], isolated_appdata)

    assert "; причина binary_content" in process.stdout


@pytest.mark.skipif(VALE is None, reason="Vale не установлен")
def test_vale_dash_ignores_line_joins_and_list_markers(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Первая строка\n- пункт списка\n", encoding="utf-8")
    config = _config(tmp_path, '[check]\nengines = ["vale"]\n')

    code, report, _ = _report(source, config, isolated_appdata)

    assert (code, report["status"]) == (0, "clean")
