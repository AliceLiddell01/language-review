"""Адаптер Vale CLI со встроенным набором правил или явно заданной конфигурацией."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from language_review.check_models import (
    CheckUnit,
    EngineRun,
    ProbeResult,
    RawFinding,
    RejectedOutput,
    UnitOutcome,
)
from language_review.config import CheckSettings

STYLE_NAME = "LanguageReview"
# Имена правил встроенного стиля → общая классификация. Правила пользовательских стилей
# относятся к style, исходное имя правила сохраняется в отчёте.
CHECK_CATEGORIES = {
    f"{STYLE_NAME}.Terminology": "terminology",
    f"{STYLE_NAME}.Terms": "terminology",
    f"{STYLE_NAME}.Jargon": "terminology",
    f"{STYLE_NAME}.Bureaucratese": "bureaucratese",
    f"{STYLE_NAME}.Repetition": "grammar",
    f"{STYLE_NAME}.Dash": "typography",
}
SEPARATOR = "\n\n"
_RE2_SPECIAL = set(r"\.+*?()|[]{}^$")
# Переменные окружения Vale могут подменить конфигурацию или каталог стилей.
_VALE_ENVIRONMENT = ("VALE_CONFIG_PATH", "VALE_STYLES_PATH")
_VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _re2_literal(value: str) -> str:
    return "".join(
        f"\\{character}" if character in _RE2_SPECIAL else character for character in value
    )


def _match_case(original: str, replacement: str) -> str:
    if original[:1].isupper() and replacement[:1].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


class ValeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ValeEngine:
    name = "vale"

    def __init__(self, settings: CheckSettings) -> None:
        self.settings = settings
        self.executable: str | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._configs: dict[tuple[tuple[str, str], ...], Path] = {}

    def describe(self) -> dict[str, Any]:
        return {
            "program": Path(self.settings.vale_program).name,
            "configuration": "custom" if self.settings.vale_config else "bundled",
            "style": None if self.settings.vale_config else STYLE_NAME,
        }

    def _resolve(self) -> str | None:
        program = self.settings.vale_program
        if Path(program).is_absolute():
            return program if Path(program).is_file() else None
        return shutil.which(program)

    def _config_for(self, terms: dict[str, str]) -> Path:
        if self.settings.vale_config:
            return Path(self.settings.vale_config)
        key = tuple(sorted(terms.items()))
        if key in self._configs:
            return self._configs[key]
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="language-review-vale-")
        root = Path(self._temporary.name) / f"config-{len(self._configs) + 1}"
        style = root / "styles" / STYLE_NAME
        style.mkdir(parents=True)
        bundled = resources.files("language_review").joinpath("vale_styles", STYLE_NAME)
        for item in bundled.iterdir():
            if item.name.endswith(".yml"):
                (style / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
        if terms:
            # JSON — подмножество YAML: значения пользователей не требуют ручного экранирования.
            rule = {
                "extends": "substitution",
                "message": "Терминология проекта: используйте %s вместо «%s».",
                "level": "warning",
                "ignorecase": True,
                "action": {"name": "replace"},
                "swap": {_re2_literal(term): value for term, value in sorted(terms.items())},
            }
            (style / "Terms.yml").write_text(
                json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (root / ".vale.ini").write_text(
            "StylesPath = styles\nMinAlertLevel = suggestion\n\n"
            f"[*.txt]\nBasedOnStyles = {STYLE_NAME}\n",
            encoding="utf-8",
        )
        self._configs[key] = root / ".vale.ini"
        return self._configs[key]

    def _invoke(self, arguments: list[str], text: str | None) -> tuple[int, str, str]:
        assert self.executable is not None
        environment = {
            key: value for key, value in os.environ.items() if key not in _VALE_ENVIRONMENT
        }
        try:
            process = subprocess.run(
                [self.executable, *arguments],
                input=None if text is None else text.encode("utf-8"),
                capture_output=True,
                timeout=self.settings.vale_timeout_seconds,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValeError("timeout", "превышено время ожидания Vale") from exc
        except OSError as exc:
            raise ValeError("launch_failed", "не удалось запустить Vale") from exc
        return (
            process.returncode,
            process.stdout.decode("utf-8", errors="replace"),
            process.stderr.decode("utf-8", errors="replace"),
        )

    def _lint(self, text: str, terms: dict[str, str]) -> list[dict[str, Any]]:
        config = self._config_for(terms)
        code, stdout, _stderr = self._invoke(
            [
                "--output=JSON",
                f"--config={config}",
                "--no-global",
                "--no-exit",
                "--ext=.txt",
            ],
            text,
        )
        try:
            document = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise ValeError("invalid_output", "Vale вернул не JSON") from exc
        if not isinstance(document, dict):
            raise ValeError("invalid_output", "Vale вернул неожиданный ответ")
        if {"Code", "Text"} <= set(document):
            # Ошибка выполнения Vale (например, E100 — неверная конфигурация); текст ошибки
            # содержит локальные пути и в отчёт не попадает.
            raise ValeError(f"vale_{document.get('Code')}", "Vale сообщил об ошибке выполнения")
        if code != 0:
            raise ValeError(f"exit_{code}", f"Vale завершился с кодом {code}")
        alerts: list[dict[str, Any]] = []
        for value in document.values():
            if not isinstance(value, list):
                raise ValeError("invalid_output", "Vale вернул неожиданный ответ")
            alerts.extend(item for item in value if isinstance(item, dict))
            if len(alerts) != len(value):
                raise ValeError("invalid_output", "Vale вернул неожиданный ответ")
        return alerts

    def probe(self) -> ProbeResult:
        self.executable = self._resolve()
        if self.executable is None:
            return ProbeResult(
                "unavailable",
                diagnostics=["program_not_found"],
                message="программа Vale не найдена",
            )
        if self.settings.vale_config and not Path(self.settings.vale_config).is_file():
            return ProbeResult(
                "unavailable",
                diagnostics=["config_not_found"],
                message="файл check.vale.config не найден",
            )
        try:
            code, stdout, _stderr = self._invoke(["--version"], None)
            match = _VERSION.search(stdout)
            if code != 0 or match is None:
                return ProbeResult(
                    "unavailable", diagnostics=["version_unavailable"], message="Vale не отвечает"
                )
            self._lint("Проверка.", {})
        except ValeError as exc:
            return ProbeResult("unavailable", diagnostics=[exc.code], message=str(exc))
        return ProbeResult("available", {"version": match.group(1)})

    def run(self, units: Sequence[CheckUnit], run: EngineRun) -> Iterator[UnitOutcome]:
        groups: dict[tuple[int, tuple[tuple[str, str], ...]], list[CheckUnit]] = {}
        for unit in units:
            groups.setdefault((unit.document_index, tuple(sorted(unit.terms.items()))), []).append(
                unit
            )
        for (_document, terms), group in groups.items():
            yield from self._run_group(group, dict(terms), run)

    def _run_group(
        self, units: list[CheckUnit], terms: dict[str, str], run: EngineRun
    ) -> Iterator[UnitOutcome]:
        # Все единицы документа проверяются одним запуском: абзацы разделены пустой строкой,
        # поэтому правила уровня предложения не соединяют соседние фрагменты.
        regions: list[tuple[int, int, CheckUnit]] = []
        parts: list[str] = []
        cursor = 0
        for unit in units:
            text = unit.views["plain"].text
            regions.append((cursor, cursor + len(text), unit))
            parts.append(text)
            cursor += len(text) + len(SEPARATOR)
        combined = SEPARATOR.join(parts)
        run.requests += 1
        try:
            alerts = self._lint(combined, terms)
        except ValeError as exc:
            run.request_failures += 1
            for unit in units:
                yield UnitOutcome(unit.index, "failed", exc.code)
            return
        line_starts = [0]
        for index, character in enumerate(combined):
            if character == "\n":
                line_starts.append(index + 1)
        outcomes = {unit.index: UnitOutcome(unit.index, "checked") for unit in units}
        for alert in alerts:
            self._map_alert(alert, combined, line_starts, regions, outcomes)
        for unit in units:
            yield outcomes[unit.index]

    def _map_alert(
        self,
        alert: dict[str, Any],
        combined: str,
        line_starts: list[int],
        regions: list[tuple[int, int, CheckUnit]],
        outcomes: dict[int, UnitOutcome],
    ) -> None:
        check = str(alert.get("Check") or "")
        message = str(alert.get("Message") or "")
        line, span, match = alert.get("Line"), alert.get("Span"), alert.get("Match")
        # Vale: строка от 1, Span — [начало, конец] в кодовых точках от 1, конец включительно.
        valid = (
            isinstance(line, int)
            and isinstance(span, list)
            and len(span) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in span)
            and 1 <= line <= len(line_starts)
        )
        start = end = -1
        if valid:
            assert isinstance(line, int) and isinstance(span, list)
            start = line_starts[line - 1] + span[0] - 1
            end = line_starts[line - 1] + span[1]
        region = next((item for item in regions if item[0] <= start < end <= item[1]), None)
        if region is None:
            target = next(
                (item for item in regions if item[0] <= max(start, 0) <= item[1]), regions[0]
            )
            outcomes[target[2].index].rejected.append(
                RejectedOutput(self.name, target[2].index, "position_out_of_range", check, message)
            )
            return
        region_start, _region_end, unit = region
        outcome = outcomes[unit.index]
        if isinstance(match, str) and match and combined[start:end] != match:
            outcome.rejected.append(
                RejectedOutput(self.name, unit.index, "position_mismatch", check, message, match)
            )
            return
        raw_action = alert.get("Action")
        action: dict[str, Any] = raw_action if isinstance(raw_action, dict) else {}
        params = action.get("Params") if action.get("Name") == "replace" else None
        original = combined[start:end]
        replacements = tuple(
            dict.fromkeys(
                _match_case(original, item) for item in (params or []) if isinstance(item, str)
            )
        )
        severity = alert.get("Severity")
        outcome.findings.append(
            RawFinding(
                engine=self.name,
                unit=unit.index,
                view="plain",
                start=start - region_start,
                end=end - region_start,
                category=CHECK_CATEGORIES.get(check, "style"),
                rule_id=check or None,
                rule_category=check.split(".", 1)[0] if "." in check else None,
                message=message,
                replacements=replacements,
                replacement_origin="rule",
                severity=str(severity) if isinstance(severity, str) else None,
                engine_excerpt=original,
            )
        )

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
            self._configs.clear()
