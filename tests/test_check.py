from __future__ import annotations

import json
import shutil
import socket
import threading
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from conftest import JsonServer, RecordedRequest
from support.check_helpers import (
    _config,
    _findings,
    _languagetool_config,
    _llm_finding,
    _run,
    _utf16,
    _vale_engine_factory,
    languagetool_responder,
    llama_responder,
)

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


def _assert_no_local_path(report: dict[str, Any], tmp_path: Path) -> None:
    serialized = json.dumps(report, ensure_ascii=False)
    # json.dumps экранирует обратную косую черту, поэтому проверяем и сериализованную форму пути.
    assert json.dumps(str(tmp_path), ensure_ascii=False)[1:-1] not in serialized
    assert str(tmp_path) not in serialized
    assert tmp_path.as_posix() not in serialized


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
    _assert_no_local_path(report, tmp_path)
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
    # Второй абзац — второй элемент пакета: предел maxItems вмещает все восемь замечаний.
    source.write_text(
        "Функция не изменяет файлы {path} и не обращается к сетю.\n\nВторой абзац.\n",
        encoding="utf-8",
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


_VALID = _llm_finding("t1", "текст", "текст.", category="style")


@pytest.mark.parametrize("provider", ["llama_cpp", "ollama"])
@pytest.mark.parametrize(
    "findings",
    [
        pytest.param([{"item": "t1"}], id="missing-fields"),
        pytest.param([{**_VALID, "severity": "high"}], id="extra-field"),
        pytest.param([{**_VALID, "excerpt": 5}], id="wrong-type"),
        pytest.param([{**_VALID, "replacement": ["текст"]}], id="wrong-replacement-type"),
        pytest.param([{**_VALID, "excerpt": ""}], id="empty-excerpt"),
        pytest.param([{**_VALID, "category": "poetry"}], id="unknown-category"),
        pytest.param([{**_VALID, "confidence": "certain"}], id="unknown-confidence"),
        pytest.param(["текст"], id="non-object-item"),
        # Один элемент — предел четыре замечания (`maxItems` пакета).
        pytest.param(
            [{**_VALID, "explanation": f"Замечание {index}."} for index in range(5)],
            id="too-many-findings",
        ),
        pytest.param([_VALID, {**_VALID, "confidence": 1}], id="valid-and-invalid"),
    ],
)
def test_llm_schema_violation_in_any_finding_fails_the_batch(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    provider: str,
    findings: list[Any],
) -> None:
    content = json.dumps({"findings": findings}, ensure_ascii=False)

    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/api/version":
            return 200, {"version": "0.0-stub"}
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "gemma4:e4b", "model": "gemma4:e4b"}]}
        if request.path == "/api/chat":
            return 200, {"message": {"content": content}, "done_reason": "stop"}
        return llama_responder(content)(request)

    json_server.responder = respond
    source = tmp_path / "notes.txt"
    source.write_text("Корректный текст\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["llm"]
[check.llm]
provider = "{provider}"
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
[check.llm.ollama]
endpoint = "{json_server.url}"
""",
    )

    code, report, stderr = _report(source, config, isolated_appdata)

    assert code == 5
    assert report["status"] == "unavailable"
    [run] = report["engines"]
    assert run["status"] == "error"
    assert "llm_schema_violation" in run["diagnostics"]
    assert run["statistics"]["units_failed"] == 1
    assert run["statistics"]["request_failures"] == 1
    document = report["documents"][0]
    assert document["status"] == "unavailable"
    assert document["fragments"][0]["checks"]["llm"] == {
        "status": "failed",
        "reason": "llm_schema_violation",
    }
    # Корректная часть ответа с нарушением не публикуется как находка.
    assert document["findings"] == [] and document["rejected"] == []
    assert "Движок llm: error" in stderr


def test_llm_structurally_valid_semantic_rejections_keep_unit_checked(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = llama_responder(
        _findings(
            _llm_finding("t7", "текст", None),
            _llm_finding("t1", "абзац", None),
            _llm_finding("t1", "текст", None, explanation="   "),
            _llm_finding("t1", "текст", "текст"),
        )
    )
    source = tmp_path / "notes.txt"
    source.write_text("Корректный текст\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 0
    assert report["status"] == "clean"
    assert report["engines"][0]["status"] == "ok"
    document = report["documents"][0]
    assert document["fragments"][0]["checks"]["llm"] == {"status": "checked", "reason": None}
    assert sorted(item["reason"] for item in document["rejected"]) == [
        "excerpt_not_found",
        "replacement_identical",
        "unknown_item",
        "value_out_of_bounds",
    ]


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


WINDOWS_GGUF = r"C:\Users\someone\AppData\Local\models\gemma-4-E4B-it-Q4_K_M.gguf"
POSIX_GGUF = "/home/someone/models/gemma-4-E4B-it-Q4_K_M.gguf"


def _llama_models(entries: list[dict[str, Any]], findings: str) -> Any:
    base = llama_responder(findings)

    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/v1/models":
            return 200, {"data": entries}
        return base(request)

    return respond


@pytest.mark.parametrize(
    ("model_id", "configured", "public"),
    [
        (WINDOWS_GGUF, None, "gemma-4-E4B-it-Q4_K_M.gguf"),
        (POSIX_GGUF, None, "gemma-4-E4B-it-Q4_K_M.gguf"),
        (WINDOWS_GGUF, WINDOWS_GGUF, "gemma-4-E4B-it-Q4_K_M.gguf"),
        ("gemma-4-e4b", None, "gemma-4-e4b"),
    ],
)
def test_llama_cpp_model_path_is_sent_but_not_published(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: JsonServer,
    model_id: str,
    configured: str | None,
    public: str,
) -> None:
    entries = [{"id": model_id, "meta": {"n_ctx": 8192}}]
    if configured is not None:
        entries.append({"id": "other-model"})
    json_server.responder = _llama_models(entries, _findings(_llm_finding("t1", "фаил", "файл")))
    source = tmp_path / "notes.txt"
    source.write_text("Сохраните фаил.\n", encoding="utf-8")
    model_line = "" if configured is None else f"model = {json.dumps(configured)}\n"
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n'
        + model_line,
    )

    code, report, stderr = _report(source, config, isolated_appdata)
    human = _run(["--config", str(config), "check", str(source)], isolated_appdata)

    assert code == 1
    [run] = report["engines"]
    assert run["details"]["model"] == public
    assert run["details"]["n_ctx"] == 8192
    requests = [item for item in json_server.requests if item.path == "/v1/chat/completions"]
    assert requests and {item.body["model"] for item in requests} == {model_id}
    assert f"модель {public}" in human.stdout
    for text in (json.dumps(report, ensure_ascii=False), stderr, human.stdout, human.stderr):
        assert "someone" not in text and "models" not in text


def test_llama_cpp_model_selection_errors_do_not_publish_paths(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    findings = '{"findings": []}'
    source = tmp_path / "notes.txt"
    source.write_text("Текст.\n", encoding="utf-8")
    endpoint = (
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n'
    )

    json_server.responder = _llama_models([{"id": WINDOWS_GGUF}, {"id": POSIX_GGUF}], findings)
    code, report, stderr = _report(source, _config(tmp_path, endpoint), isolated_appdata)
    assert code == 5
    assert "model_not_selected" in report["engines"][0]["diagnostics"]
    assert report["engines"][0]["details"]["model"] is None

    json_server.responder = _llama_models([{"id": "gemma-4-e4b"}], findings)
    missing = endpoint + f"model = {json.dumps(POSIX_GGUF)}\n"
    code, missing_report, missing_stderr = _report(
        source, _config(tmp_path, missing), isolated_appdata
    )
    assert code == 5
    [run] = missing_report["engines"]
    assert "model_not_found" in run["diagnostics"]
    assert run["details"]["model"] == "gemma-4-E4B-it-Q4_K_M.gguf"
    assert "gemma-4-E4B-it-Q4_K_M.gguf" in run["message"]
    for text in (json.dumps([report, missing_report]), stderr, missing_stderr):
        assert "someone" not in text
    assert not any(item.path == "/v1/chat/completions" for item in json_server.requests)


def test_document_configuration_reports_effective_profile_and_cli_settings(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    lt = languagetool_responder({"фаил": ["файл"]})

    def llm(items: list[dict[str, Any]]) -> str:
        return _findings(
            *(
                _llm_finding(item["id"], "фаил", "файл", category="style")
                for item in items
                if "фаил" in item["text"]
            )
        )

    llama = llama_responder(llm)

    def respond(request: RecordedRequest) -> tuple[int, Any]:
        return lt(request) if request.path.startswith("/v2/") else llama(request)

    json_server.responder = respond
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("Сохраните фаил.\n", encoding="utf-8")
    (root / "notes.txt").write_text("Откройте фаил.\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f"""[check]
engines = ["languagetool", "llm"]
categories = ["spelling", "style"]
terms = {{ "фреймворк" = "каркас" }}
[check.languagetool]
endpoint = "{json_server.url}"
[check.llm.llama_cpp]
endpoint = "{json_server.url}"
[profiles.docs]
match_paths = ["docs/**"]
[profiles.docs.check]
engines = ["llm"]
categories = ["spelling"]
terms = {{ "апдейт" = "обновление" }}
""",
    )

    code, report, _stderr = _report(root, config, isolated_appdata)

    assert code == 1
    assert report["configuration"]["engines"] == ["languagetool", "llm"]
    assert report["configuration"]["categories"] == ["spelling", "style"]
    documents = {item["path"]: item for item in report["documents"]}
    docs, notes = documents["docs/guide.md"], documents["notes.txt"]
    assert docs["profile"] == "docs" and notes["profile"] is None
    assert docs["configuration"] == {
        "engines": ["llm"],
        "categories": ["spelling"],
        "terms_count": 2,
        "sources": {
            "engines": "профиль docs",
            "categories": "профиль docs",
            "terms": "файл конфигурации; профиль docs",
        },
    }
    assert notes["configuration"] == {
        "engines": ["languagetool", "llm"],
        "categories": ["spelling", "style"],
        "terms_count": 1,
        "sources": {
            "engines": "файл конфигурации",
            "categories": "файл конфигурации",
            "terms": "файл конфигурации",
        },
    }
    # Отражённые настройки совпадают с примененными: замечание style отфильтровано
    # только в профиле docs, LanguageTool не проверял документ профиля.
    assert set(docs["fragments"][0]["checks"]) == {"llm"}
    assert docs["findings"] == []
    assert sorted(item["category"] for item in notes["findings"]) == ["spelling", "style"]
    llm_run = next(item for item in report["engines"] if item["name"] == "llm")
    assert llm_run["statistics"]["filtered"] == 1
    prompts = {}
    for item in json_server.requests:
        if item.path == "/v1/chat/completions":
            system, user = item.body["messages"]
            prompts[json.loads(user["content"])["items"][0]["text"]] = system["content"]
    assert "апдейт" in prompts["Сохраните фаил."] and "апдейт" not in prompts["Откройте фаил."]
    assert "апдейт" not in json.dumps(report, ensure_ascii=False)
    _assert_no_local_path(report, tmp_path)

    _cli_code, overridden, _ = _report(
        root, config, isolated_appdata, "--engine", "llm", "--category", "style"
    )
    for document in overridden["documents"]:
        assert document["configuration"]["engines"] == ["llm"]
        assert document["configuration"]["categories"] == ["style"]
        assert document["configuration"]["sources"]["categories"] == "аргумент командной строки"
    assert overridden["configuration"]["categories"] == ["style"]


def test_document_configuration_without_profile_matches_base(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = languagetool_responder({})
    source = tmp_path / "notes.txt"
    source.write_text("Текст.\n", encoding="utf-8")

    _code, report, _ = _report(
        source, _languagetool_config(tmp_path, json_server.url), isolated_appdata
    )

    [document] = report["documents"]
    assert document["configuration"] == {
        "engines": report["configuration"]["engines"],
        "categories": report["configuration"]["categories"],
        "terms_count": 0,
        "sources": {
            "engines": "файл конфигурации",
            "categories": "по умолчанию",
            "terms": "по умолчанию",
        },
    }


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
    _assert_no_local_path(report, tmp_path)


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


def _broken_match(**override: Any) -> dict[str, Any]:
    match: dict[str, Any] = {
        "message": "Опечатка.",
        "offset": 0,
        "length": 6,
        "replacements": [{"value": "Ошибка"}],
        "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "category": {"id": "TYPOS"}},
    }
    match.update(override)
    return match


@pytest.mark.parametrize(
    "broken",
    [
        _broken_match(replacements=None),
        _broken_match(offset="0"),
        _broken_match(offset=500),
        "не объект",
    ],
    ids=["replacements_none", "offset_string", "offset_beyond_text", "match_not_object"],
)
def test_languagetool_schema_violation_is_incomplete_not_clean(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer, broken: Any
) -> None:
    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/v2/languages":
            return 200, [{"name": "Russian", "code": "ru", "longCode": "ru-RU"}]
        text = request.body["text"]
        matches = [broken] if text.startswith("Ошыбка") else []
        if text.startswith("Второй"):
            matches = [_broken_match(offset=7, length=4, replacements=[{"value": "файл"}])]
        return 200, {"software": {"version": "stub"}, "matches": matches}

    json_server.responder = respond
    source = tmp_path / "notes.txt"
    source.write_text("Ошыбка в слове.\n\nВторой фаил лежит рядом.\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, stderr = _report(source, config, isolated_appdata)

    assert (code, report["status"]) == (4, "partial")
    assert report["engines"][0]["status"] == "partial"
    document = report["documents"][0]
    assert document["status"] == "partial"
    # Корректное замечание из другой единицы не теряется.
    assert [item["excerpt"] for item in document["findings"]] == ["фаил"]
    checks = [item["checks"]["languagetool"] for item in document["fragments"]]
    assert checks == [
        {"status": "failed", "reason": "languagetool_schema_violation"},
        {"status": "checked", "reason": None},
    ]
    assert "languagetool_schema_violation" in report["engines"][0]["diagnostics"]
    assert "Неполная проверка" in stderr


def test_languagetool_schema_violation_in_only_unit_is_engine_error(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    def respond(request: RecordedRequest) -> tuple[int, Any]:
        if request.path == "/v2/languages":
            return 200, [{"name": "Russian", "code": "ru", "longCode": "ru-RU"}]
        return 200, {"software": {"version": "stub"}, "matches": [_broken_match(replacements=None)]}

    json_server.responder = respond
    source = tmp_path / "notes.txt"
    source.write_text("Ошыбка в слове.\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert (code, report["status"]) == (5, "unavailable")
    assert report["engines"][0]["status"] == "error"
    assert report["documents"][0]["findings"] == []
    assert [item["reason"] for item in report["documents"][0]["rejected"]] == ["schema_violation"]


def test_languagetool_valid_match_with_rejected_suggestion_stays_checked(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = languagetool_responder({"ошыбка": ["не ошибка"]})
    source = tmp_path / "notes.txt"
    source.write_text("Здесь ошыбка.\n", encoding="utf-8")
    config = _languagetool_config(tmp_path, json_server.url)

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert (code, report["status"]) == (1, "findings")
    assert report["engines"][0]["status"] == "ok"
    finding = report["documents"][0]["findings"][0]
    assert finding["suggestions"][0]["status"] == "rejected"
    assert "negation_changed" in finding["suggestions"][0]["reasons"]


@pytest.mark.parametrize(
    ("alert", "clean"),
    [
        ({"Check": "X.Y", "Message": "m", "Line": "1", "Span": [1, 3]}, False),
        ({"Check": "X.Y", "Message": "m", "Line": 1, "Span": [1]}, False),
        ({"Check": "X.Y", "Message": "m", "Line": 1, "Span": [1, 3], "Match": "нет"}, False),
    ],
    ids=["line_not_int", "span_short", "match_mismatch_fails_only_affected_unit"],
)
def test_vale_malformed_alert_is_not_clean(
    tmp_path: Path, alert: dict[str, Any], clean: bool
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Здесь ошыбка.\n", encoding="utf-8")
    config = load_config(_config(tmp_path, '[check]\nengines = ["vale"]\n'))
    stdout = json.dumps({"stdin.txt": [alert]})

    result = check_paths([str(source)], config, engine_factory=_vale_engine_factory(stdout))

    document = result.documents[0]
    if clean:
        assert (result.status, document.status) == ("clean", "clean")
        assert [item.reason for item in document.rejected] == ["position_mismatch"]
    else:
        assert result.status in {"partial", "unavailable"}
        assert document.status != "clean"
        assert document.unit_status[(0, "vale")] == ("failed", "vale_schema_violation")


def test_vale_match_mismatch_fails_only_the_affected_unit(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Здесь ошыбка.\n\nЗдесь ошыбка.\n", encoding="utf-8")
    config = load_config(_config(tmp_path, '[check]\nengines = ["vale"]\n'))
    alerts = [
        {
            "Check": "X.Y",
            "Message": "Некорректная цитата",
            "Line": 1,
            "Span": [7, 12],
            "Match": "ошибка",
        },
        {
            "Check": "X.Y",
            "Message": "Корректная цитата",
            "Line": 3,
            "Span": [7, 12],
            "Match": "ошыбка",
        },
    ]

    result = check_paths(
        [str(source)],
        config,
        engine_factory=_vale_engine_factory(json.dumps({"stdin.txt": alerts})),
    )

    document = result.documents[0]
    [run] = result.engines
    assert document.unit_status[(0, "vale")] == ("failed", "vale_schema_violation")
    assert document.unit_status[(1, "vale")] == ("checked", None)
    assert (run.units_failed, run.units_checked) == (1, 1)
    assert run.status == "partial"


@pytest.mark.parametrize(
    "match,span_end,checked",
    [
        ("Слово.\n\nСлово", 13, True),
        ("Неверная цитата", 13, False),
        ("Слово.\n\nСлово", 1000, False),
    ],
)
def test_vale_multiline_separator_match_is_rejected_without_document_failure(
    tmp_path: Path,
    match: str,
    span_end: int,
    checked: bool,
) -> None:
    source = tmp_path / "notes.txt"
    original = "Слово.\n\nСлово ошыбка.\n".encode()
    source.write_bytes(original)
    config = load_config(_config(tmp_path, '[check]\nengines=["vale"]\n'))
    alert = {
        "Check": "LanguageReview.Repetition",
        "Message": "Повтор",
        "Line": 1,
        "Span": [1, span_end],
        "Match": match,
    }
    result = check_paths(
        [str(source)],
        config,
        engine_factory=_vale_engine_factory(json.dumps({"stdin.txt": [alert]})),
    )
    [document], [run] = result.documents, result.engines
    assert (run.units_checked, run.units_failed) == ((2, 0) if checked else (0, 2))
    assert [item.reason for item in document.rejected] == ["position_out_of_range"]
    assert not document.findings
    assert run.status == ("ok" if checked else "error")
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "payload,valid",
    [
        ({}, False),
        ({"Match": None}, False),
        ({"Match": 42}, False),
        ({"Match": ""}, False),
        ({"Match": "ошибка"}, False),
        ({"Match": "ошыбка"}, True),
    ],
    ids=["missing", "null", "number", "empty", "mismatch", "exact"],
)
def test_vale_requires_exact_match_for_nonempty_span(
    tmp_path: Path, payload: dict[str, Any], valid: bool
) -> None:
    source = tmp_path / "notes.txt"
    original = "Здесь ошыбка.\n\nЗдесь ошыбка.\n".encode()
    source.write_bytes(original)
    config = load_config(_config(tmp_path, '[check]\nengines=["vale"]\n'))
    alert = {"Check": "X.Rule", "Message": "Гипотеза", "Line": 1, "Span": [7, 12]}
    alerts = [{**alert, **payload}, {**alert, "Line": 3, "Match": "ошыбка"}]
    result = check_paths(
        [str(source)],
        config,
        engine_factory=_vale_engine_factory(json.dumps({"stdin.txt": alerts})),
    )
    [document], [run] = result.documents, result.engines
    assert document.unit_status[(1, "vale")] == ("checked", None)
    assert (run.units_checked, run.units_failed, run.rejected) == (
        (2, 0, 0) if valid else (1, 1, 1)
    )
    assert document.status == ("findings" if valid else "partial")
    assert run.status == ("ok" if valid else "partial")
    assert len(document.findings) == (2 if valid else 1)
    if not valid:
        assert document.unit_status[(0, "vale")] == ("failed", "vale_schema_violation")
        assert [item.reason for item in document.rejected] == ["position_mismatch"]
    assert source.read_bytes() == original


ENGLISH_SENTENCE = "The tool reads the configuration file from disk."


def _english_llm(findings: Any) -> Any:
    """Ответ модели только для английских элементов; русские элементы без замечаний."""

    def respond(items: list[dict[str, Any]]) -> str:
        return _findings(
            *(
                finding
                for item in items
                if item.get("language") == "en"
                for finding in findings(item)
            )
        )

    return respond


def _chat_items(json_server: JsonServer) -> list[dict[str, Any]]:
    return [
        item
        for request in json_server.requests
        if request.path == "/v1/chat/completions"
        for item in json.loads(request.body["messages"][1]["content"])["items"]
    ]


def test_english_prose_reaches_llm_but_not_russian_language_engines(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    lt = languagetool_responder({"ошыбка": ["ошибка"]})
    llama = llama_responder(
        _english_llm(
            lambda item: [
                _llm_finding(
                    item["id"],
                    item["text"],
                    None,
                    category="untranslated_english",
                    explanation="Комментарий в русскоязычном проекте написан по-английски.",
                    confidence="medium",
                )
            ]
        )
    )

    def respond(request: RecordedRequest) -> tuple[int, Any]:
        return lt(request) if request.path.startswith("/v2/") else llama(request)

    json_server.responder = respond
    source = tmp_path / "notes.txt"
    source.write_text(
        f"Здесь ошыбка в слове.\n\n{ENGLISH_SENTENCE}\n\ngit status --short\n\n--verbose\n",
        encoding="utf-8",
    )
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

    assert (code, report["status"]) == (1, "findings")
    document = report["documents"][0]
    english = [item for item in document["findings"] if item["category"] == "untranslated_english"]
    assert [item["excerpt"] for item in english] == [ENGLISH_SENTENCE]
    # replacement: null допустим — замечание без варианта замены.
    assert english[0]["suggestions"] == []
    assert english[0]["agreement"] == ["llm"]
    checks = [
        (item["checks"]["languagetool"], item["checks"]["llm"]) for item in document["fragments"]
    ]
    skipped = {"status": "skipped", "reason": "no_russian_text"}
    done = {"status": "checked", "reason": None}
    assert checks == [(done, done), (skipped, done), (skipped, skipped), (skipped, skipped)]
    assert report["summary"]["units_without_russian_text"] == 3
    languagetool_texts = [
        item.body["text"] for item in json_server.requests if item.path == "/v2/check"
    ]
    assert all("configuration" not in text for text in languagetool_texts)
    items = _chat_items(json_server)
    assert [item["text"] for item in items if item.get("language") == "en"] == [ENGLISH_SENTENCE]
    assert all("language" not in item for item in items if "ошыбка" in item["text"])
    assert not any("--verbose" in item["text"] or "git status" in item["text"] for item in items)


def test_english_translation_is_checked_for_dropped_negation_and_acronyms(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    sentences = {
        "The tool does not delete files from the disk.": "Утилита удаляет файлы с диска.",
        "The tool does not delete files from the disk twice.": "Утилита не удаляет файлы дважды.",
        "The JSON parser reads the configuration file.": "Разбор читает файл настройки.",
        "The parser reads the configuration file.": "Разбор читает файл настройки.",
    }
    json_server.responder = llama_responder(
        _english_llm(
            lambda item: [
                _llm_finding(
                    item["id"],
                    item["text"],
                    sentences[item["text"]],
                    category="untranslated_english",
                    explanation="Текст написан по-английски.",
                )
            ]
        )
    )
    source = tmp_path / "notes.txt"
    source.write_text("\n\n".join(sentences) + "\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert code == 1
    statuses = {
        item["excerpt"]: (item["suggestions"][0]["status"], item["suggestions"][0]["reasons"])
        for item in report["documents"][0]["findings"]
    }
    assert statuses == {
        "The tool does not delete files from the disk.": ("rejected", ["negation_changed"]),
        "The tool does not delete files from the disk twice.": ("candidate", []),
        "The JSON parser reads the configuration file.": (
            "rejected",
            ["technical_content_changed"],
        ),
        "The parser reads the configuration file.": ("candidate", []),
    }


def test_english_items_accept_only_untranslated_english_category(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = llama_responder(
        _english_llm(
            lambda item: [
                _llm_finding(item["id"], "configuration", "конфигурация", category="spelling")
            ]
        )
    )
    source = tmp_path / "notes.txt"
    source.write_text(f"{ENGLISH_SENTENCE}\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert (code, report["status"]) == (0, "clean")
    document = report["documents"][0]
    assert [item["reason"] for item in document["rejected"]] == ["category_not_applicable"]
    assert document["fragments"][0]["checks"]["llm"] == {"status": "checked", "reason": None}


def test_disabled_category_does_not_check_english_hidden(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = llama_responder('{"findings": []}')
    source = tmp_path / "notes.txt"
    source.write_text(f"{ENGLISH_SENTENCE}\n", encoding="utf-8")
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, _stderr = _report(source, config, isolated_appdata, "--category", "grammar")

    assert (code, report["status"]) == (0, "clean")
    assert report["documents"][0]["fragments"][0]["checks"]["llm"] == {
        "status": "skipped",
        "reason": "category_disabled",
    }
    assert _chat_items(json_server) == []


def test_english_markdown_prose_and_code_fragments(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    json_server.responder = llama_responder('{"findings": []}')
    source = tmp_path / "guide.md"
    source.write_text(
        "# Руководство\n\nКоманда `cargo build` собирает проект.\n\n"
        "Use the `HttpClient.GetAsync` method to fetch the page.\n\n"
        "```text\nThis block is code and must stay untouched here.\n```\n",
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        f'[check]\nengines = ["llm"]\n[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n',
    )

    code, report, _stderr = _report(source, config, isolated_appdata)

    assert (code, report["status"]) == (0, "clean")
    english = [item["text"] for item in _chat_items(json_server) if item.get("language") == "en"]
    assert len(english) == 1
    assert "method to fetch the page" in english[0]
    assert not any("must stay untouched" in item["text"] for item in _chat_items(json_server))
    assert all(
        item["checks"]["llm"]["status"] in {"checked", "skipped"}
        for item in report["documents"][0]["fragments"]
    )


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
