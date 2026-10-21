"""Общие протокольные ответы движков и запуск настоящего CLI для тестов."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import RecordedRequest

from language_review.engine_vale import ValeEngine


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


def _vale_engine_factory(stdout: str) -> Any:
    class FakeVale(ValeEngine):
        def _resolve(self) -> str | None:
            return "vale"

        def _invoke(self, arguments: list[str], text: str | None) -> tuple[int, str, str]:
            if arguments == ["--version"]:
                return 0, "vale version 3.0.0", ""
            return 0, stdout if text and "ошыбка" in text else "{}", ""

    def factory(name: str, settings: Any) -> Any:
        assert name == "vale"
        return FakeVale(settings)

    return factory
