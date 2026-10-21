"""Приёмочные проверки на настоящих движках.

Запускаются, только если задан адрес соответствующего локального сервиса:
`LANGUAGE_REVIEW_LIVE_LANGUAGETOOL`, `LANGUAGE_REVIEW_LIVE_LLAMA_CPP`
(+ необязательный `LANGUAGE_REVIEW_LIVE_LLAMA_CPP_MODEL`), `LANGUAGE_REVIEW_LIVE_OLLAMA`
и `LANGUAGE_REVIEW_LIVE_OLLAMA_MODEL`. Vale используется, если программа найдена в PATH.
Без сервиса тест пропускается и не считается пройденным.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "check"
SCHEMA = json.loads((ROOT / "docs" / "check-report.schema.json").read_text(encoding="utf-8"))
LANGUAGETOOL = os.environ.get("LANGUAGE_REVIEW_LIVE_LANGUAGETOOL")
LLAMA_CPP = os.environ.get("LANGUAGE_REVIEW_LIVE_LLAMA_CPP")
LLAMA_CPP_MODEL = os.environ.get("LANGUAGE_REVIEW_LIVE_LLAMA_CPP_MODEL", "")
OLLAMA = os.environ.get("LANGUAGE_REVIEW_LIVE_OLLAMA")
OLLAMA_MODEL = os.environ.get("LANGUAGE_REVIEW_LIVE_OLLAMA_MODEL", "gemma4:e4b")
VALE = shutil.which("vale")
POSITIVE = "Программа сохраняет настройки в фаил конфигурации.\n"
CLEAN = "Функция не изменяет входные данные и не обращается к сети.\n"


def _run(args: list[str], appdata: Path) -> subprocess.CompletedProcess[str]:
    from support.project_inputs import prepare_cli

    prepare_cli(args, appdata)
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
        timeout=900,
    )


def _check(path: Path, config: Path, appdata: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    process = _run(
        ["--config", str(config), "check", str(path), "--format", "json", *extra], appdata
    )
    report = json.loads(process.stdout)
    jsonschema.Draft202012Validator(SCHEMA).validate(report)
    return process.returncode, report


def prepare_example_inputs(target: Path) -> Path:
    """Входы эталонного примера: LF для Rust, BOM и CRLF для Markdown — независимо от Git."""
    repo = target / "repo"
    repo.mkdir()
    rust = (FIXTURES / "sample.rs").read_bytes().replace(b"\r\n", b"\n")
    (repo / "sample.rs").write_bytes(rust)
    markdown = (FIXTURES / "sample.md").read_bytes().replace(b"\r\n", b"\n")
    (repo / "sample.md").write_bytes(b"\xef\xbb\xbf" + markdown.replace(b"\n", b"\r\n"))
    return repo


def _normalized(report: dict[str, Any]) -> dict[str, Any]:
    report = json.loads(json.dumps(report))
    report["tool"].pop("version")
    for engine in report["engines"]:
        engine.pop("elapsed_ms")
        engine["details"].pop("endpoint", None)
    return report


@pytest.mark.skipif(LANGUAGETOOL is None or VALE is None, reason="нужны LanguageTool и Vale")
def test_check_report_example_matches_real_cli_output(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    repo = prepare_example_inputs(tmp_path)
    before = {item.name: item.read_bytes() for item in repo.iterdir()}
    config = tmp_path / "config.toml"
    config.write_text(
        (FIXTURES / "example-config.toml").read_text(encoding="utf-8")
        + f'[check.languagetool]\nendpoint = "{LANGUAGETOOL}"\n',
        encoding="utf-8",
    )

    code, report = _check(repo, config, isolated_appdata)

    expected = json.loads((ROOT / "docs" / "check-report.example.json").read_text(encoding="utf-8"))
    versions = {item["name"]: item["details"].get("version") for item in report["engines"]}
    expected_versions = {
        item["name"]: item["details"].get("version") for item in expected["engines"]
    }
    if versions != expected_versions:
        pytest.skip(f"эталон построен на {expected_versions}, доступны {versions}")
    assert code == 1
    assert _normalized(report) == _normalized(expected)
    assert {item.name: item.read_bytes() for item in repo.iterdir()} == before
    # Ни одно предложение не вставляет пробел внутрь `id.ToString()`.
    serialized = json.dumps(report, ensure_ascii=False)
    assert "id. ToString" not in serialized


@pytest.mark.skipif(LANGUAGETOOL is None, reason="LANGUAGE_REVIEW_LIVE_LANGUAGETOOL не задан")
def test_languagetool_live_positive_clean_and_code_safety(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[check]\nengines = ["languagetool"]\n[check.languagetool]\nendpoint = "{LANGUAGETOOL}"\n',
        encoding="utf-8",
    )
    positive = tmp_path / "positive.rs"
    original = "// 😀 Вызовите id.ToString() чтобы праверить строку.\nfn main() {}\n".encode()
    positive.write_bytes(original)
    clean = tmp_path / "clean.md"
    clean.write_text(CLEAN, encoding="utf-8")

    code, report = _check(positive, config, isolated_appdata)
    clean_code, clean_report = _check(clean, config, isolated_appdata)

    assert code == 1
    findings = report["documents"][0]["findings"]
    by_excerpt = {item["excerpt"]: item for item in findings}
    spelling = by_excerpt["праверить"]
    start, end = spelling["range"]["start"], spelling["range"]["end"]
    assert original[start["byte_offset"] : end["byte_offset"]].decode() == "праверить"
    assert start["utf16_column"] == start["column"] + 1
    # Запятая перед «чтобы» вставляется после токена, а сам токен остаётся нетронутым.
    edits = [
        suggestion["edit"]
        for finding in findings
        for suggestion in finding["suggestions"]
        if suggestion["status"] == "candidate"
    ]
    assert all("ToString" not in (edit["original"] or "") for edit in edits)
    assert "id. ToString" not in json.dumps(report, ensure_ascii=False)
    assert positive.read_bytes() == original
    assert (clean_code, clean_report["status"]) == (0, "clean")


def _llm_config(tmp_path: Path, provider: str, endpoint: str, model: str) -> Path:
    config = tmp_path / f"{provider}.toml"
    config.write_text(
        f'[check]\nengines = ["llm"]\n[check.llm]\nprovider = "{provider}"\n'
        f'[check.llm.{provider}]\nendpoint = "{endpoint}"\nmodel = "{model}"\n',
        encoding="utf-8",
    )
    return config


def _assert_llm_positive_and_clean(
    tmp_path: Path, appdata: Path, config: Path, *, require_clean: bool
) -> None:
    positive = tmp_path / "positive.md"
    positive.write_text(POSITIVE, encoding="utf-8")
    clean = tmp_path / "clean.md"
    clean.write_text(CLEAN, encoding="utf-8")

    code, report = _check(positive, config, appdata)
    clean_code, clean_report = _check(clean, config, appdata)

    assert code == 1, report["engines"]
    finding = next(
        item for item in report["documents"][0]["findings"] if "фаил" in (item["excerpt"] or "")
    )
    assert finding["excerpt_status"] == "verified"
    assert finding["fragment_ids"] == [report["documents"][0]["fragments"][0]["id"]]
    start = finding["range"]["start"]
    assert POSITIVE.encode()[start["byte_offset"] :].startswith(finding["excerpt"].encode())
    assert clean_report["engines"][0]["status"] == "ok"
    if require_clean:
        assert (clean_code, clean_report["status"]) == (0, "clean")


@pytest.mark.skipif(LLAMA_CPP is None, reason="LANGUAGE_REVIEW_LIVE_LLAMA_CPP не задан")
def test_llama_cpp_live_positive_clean_and_unavailable(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    assert LLAMA_CPP is not None
    config = _llm_config(tmp_path, "llama_cpp", LLAMA_CPP, LLAMA_CPP_MODEL)

    _assert_llm_positive_and_clean(tmp_path, isolated_appdata, config, require_clean=True)

    down = _llm_config(tmp_path, "llama_cpp", "http://127.0.0.1:9", LLAMA_CPP_MODEL)
    code, report = _check(tmp_path / "clean.md", down, isolated_appdata)
    assert (code, report["status"]) == (5, "unavailable")


@pytest.mark.skipif(OLLAMA is None, reason="LANGUAGE_REVIEW_LIVE_OLLAMA не задан")
def test_ollama_live_same_logical_task(tmp_path: Path, isolated_appdata: Path) -> None:
    assert OLLAMA is not None
    config = _llm_config(tmp_path, "ollama", OLLAMA, OLLAMA_MODEL)

    # Ollama применяет собственный шаблон чата; ложные замечания на чистом тексте фиксируются
    # в отчёте, но положительный случай и отображение координат обязаны совпадать.
    _assert_llm_positive_and_clean(tmp_path, isolated_appdata, config, require_clean=False)


@pytest.mark.skipif(LLAMA_CPP is None, reason="LANGUAGE_REVIEW_LIVE_LLAMA_CPP не задан")
def test_llama_cpp_live_english_prose_reaches_model_and_stays_unchanged(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    assert LLAMA_CPP is not None
    config = _llm_config(tmp_path, "llama_cpp", LLAMA_CPP, LLAMA_CPP_MODEL)
    notes = tmp_path / "notes.txt"
    original = (
        "Программа сохраняет настройки.\n\n"
        "The tool reads the configuration file from disk before it starts.\n\n"
        "git status --short\n"
    ).encode()
    notes.write_bytes(original)

    code, report = _check(notes, config, isolated_appdata)

    # Вердикт модели о английском абзаце не фиксируется, но абзац обязан быть проверен,
    # а команда без прозы — честно пропущена.
    assert code in {0, 1}, report["engines"]
    checks = [item["checks"]["llm"] for item in report["documents"][0]["fragments"]]
    assert checks[0]["status"] == checks[1]["status"] == "checked"
    assert checks[2] == {"status": "skipped", "reason": "no_russian_text"}
    for finding in report["documents"][0]["findings"]:
        start = finding["range"]["start"]
        assert original[start["byte_offset"] :].decode().startswith(finding["excerpt"])
    assert notes.read_bytes() == original
