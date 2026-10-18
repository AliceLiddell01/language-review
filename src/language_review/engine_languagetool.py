"""Адаптер локального сервера LanguageTool (`POST /v2/check`)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
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
from language_review.engine_http import (
    EngineHttpError,
    endpoint_origin,
    join_url,
    request_json,
    safe_endpoint,
)

# Категории LanguageTool → общая классификация; исходная категория сохраняется в отчёте.
CATEGORY_MAP = {
    "TYPOS": "spelling",
    "CASING": "spelling",
    "COMPOUNDING": "spelling",
    "GRAMMAR": "grammar",
    "CONFUSED_WORDS": "grammar",
    "COLLOCATIONS": "grammar",
    "SEMANTICS": "grammar",
    "PUNCTUATION": "punctuation",
    "TYPOGRAPHY": "typography",
    "STYLE": "style",
    "REDUNDANCY": "style",
    "PLAIN_ENGLISH": "style",
}
# Отдельные правила, чья категория LanguageTool не совпадает с сутью замечания.
RULE_CATEGORY_OVERRIDES = {"WORD_REPEAT_RULE": "grammar"}
MAX_UNIT_CHARS = 20_000
MAX_REPLACEMENTS = 5
# Начало фрагмента кода или строки таблицы не обязано быть началом предложения.
BOUNDARY_RULES = {"UPPERCASE_SENTENCE_START"}
CONSECUTIVE_FAILURE_LIMIT = 3


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class LanguageToolEngine:
    name = "languagetool"

    def __init__(self, settings: CheckSettings) -> None:
        self.settings = settings
        self.endpoint = settings.languagetool_endpoint

    def describe(self) -> dict[str, Any]:
        return {
            "endpoint": safe_endpoint(self.endpoint),
            "endpoint_origin": endpoint_origin(self.endpoint),
            "language": self.settings.languagetool_language,
            "level": self.settings.languagetool_level,
            "disabled_rules": list(self.settings.languagetool_disabled_rules),
        }

    def probe(self) -> ProbeResult:
        timeout = min(self.settings.languagetool_timeout_seconds, 10.0)
        try:
            languages = request_json(join_url(self.endpoint, "/v2/languages"), timeout=timeout)
        except EngineHttpError as exc:
            return ProbeResult("unavailable", diagnostics=[exc.code], message=str(exc))
        language = self.settings.languagetool_language
        codes = (
            {
                str(item.get(key))
                for item in languages.body
                if isinstance(item, dict)
                for key in ("code", "longCode")
            }
            if isinstance(languages.body, list)
            else set()
        )
        if language not in codes:
            return ProbeResult(
                "unavailable",
                diagnostics=["language_not_supported"],
                message=f"сервер не поддерживает язык {language}",
            )
        try:
            reply = request_json(
                join_url(self.endpoint, "/v2/check"),
                form={"language": language, "text": "Проверка."},
                timeout=timeout,
            )
        except EngineHttpError as exc:
            return ProbeResult("unavailable", diagnostics=[exc.code], message=str(exc))
        software = reply.body.get("software") if isinstance(reply.body, dict) else None
        version = software.get("version") if isinstance(software, dict) else None
        return ProbeResult("available", {"version": str(version) if version else None})

    def run(self, units: Sequence[CheckUnit], run: EngineRun) -> Iterator[UnitOutcome]:
        failures = 0
        for unit in units:
            if failures >= CONSECUTIVE_FAILURE_LIMIT:
                yield UnitOutcome(unit.index, "failed", "engine_aborted")
                continue
            view = unit.views["plain"]
            if len(view.text) > MAX_UNIT_CHARS:
                yield UnitOutcome(unit.index, "skipped", "unit_too_large")
                continue
            form = {
                "language": self.settings.languagetool_language,
                "text": view.text,
                "level": self.settings.languagetool_level,
            }
            if self.settings.languagetool_disabled_rules:
                form["disabledRules"] = ",".join(self.settings.languagetool_disabled_rules)
            run.requests += 1
            try:
                reply = request_json(
                    join_url(self.endpoint, "/v2/check"),
                    form=form,
                    timeout=self.settings.languagetool_timeout_seconds,
                )
            except EngineHttpError as exc:
                failures += 1
                run.request_failures += 1
                yield UnitOutcome(unit.index, "failed", exc.code)
                continue
            failures = 0
            yield self._outcome(unit, reply.body)

    def _outcome(self, unit: CheckUnit, body: Any) -> UnitOutcome:
        view = unit.views["plain"]
        if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
            return UnitOutcome(unit.index, "failed", "languagetool_protocol_error")
        outcome = UnitOutcome(unit.index, "checked")
        warnings = body.get("warnings")
        if isinstance(warnings, dict) and warnings.get("incompleteResults") is True:
            outcome.status = "failed"
            outcome.reason = "languagetool_incomplete_results"
        for match in body["matches"]:
            if not isinstance(match, dict):
                outcome.rejected.append(RejectedOutput(self.name, unit.index, "schema_violation"))
                continue
            rule = _mapping(match.get("rule"))
            rule_id = str(rule.get("id")) if rule.get("id") is not None else None
            category_info = _mapping(rule.get("category"))
            rule_category = (
                str(category_info.get("id")) if category_info.get("id") is not None else None
            )
            message = str(match["message"]) if isinstance(match.get("message"), str) else ""
            offset, length = match.get("offset"), match.get("length")
            if (
                not isinstance(offset, int)
                or not isinstance(length, int)
                or isinstance(offset, bool)
                or isinstance(length, bool)
                or offset < 0
                or length < 0
            ):
                outcome.rejected.append(
                    RejectedOutput(self.name, unit.index, "schema_violation", rule_id, message)
                )
                continue
            # LanguageTool считает позиции в кодовых единицах UTF-16, а не в кодовых точках.
            start = view.utf16_index(offset)
            end = view.utf16_index(offset + length)
            if start is None or end is None:
                outcome.rejected.append(
                    RejectedOutput(self.name, unit.index, "position_out_of_range", rule_id, message)
                )
                continue
            if start == end:
                outcome.rejected.append(
                    RejectedOutput(self.name, unit.index, "empty_range", rule_id, message)
                )
                continue
            if rule_id in BOUNDARY_RULES and start == 0 and not unit.sentence_start:
                outcome.rejected.append(
                    RejectedOutput(
                        self.name,
                        unit.index,
                        "fragment_boundary_artifact",
                        rule_id,
                        message,
                        view.text[start:end],
                    )
                )
                continue
            issue_type = rule.get("issueType")
            if rule_id in RULE_CATEGORY_OVERRIDES:
                category = RULE_CATEGORY_OVERRIDES[rule_id]
            elif issue_type == "misspelling":
                category = "spelling"
            else:
                category = CATEGORY_MAP.get(rule_category or "", "other")
            raw_replacements = match.get("replacements", [])
            if not isinstance(raw_replacements, list):
                outcome.rejected.append(
                    RejectedOutput(self.name, unit.index, "schema_violation", rule_id, message)
                )
                continue
            replacements = tuple(
                dict.fromkeys(
                    item["value"]
                    for item in raw_replacements
                    if isinstance(item, dict) and isinstance(item.get("value"), str)
                )
            )[:MAX_REPLACEMENTS]
            dictionary = issue_type == "misspelling" or (rule_id or "").startswith("MORFOLOGIK")
            outcome.findings.append(
                RawFinding(
                    engine=self.name,
                    unit=unit.index,
                    view="plain",
                    start=start,
                    end=end,
                    category=category,
                    rule_id=rule_id,
                    rule_category=rule_category,
                    message=message,
                    replacements=replacements,
                    replacement_origin="dictionary" if dictionary else "rule",
                    engine_excerpt=view.text[start:end],
                )
            )
        return outcome

    def close(self) -> None:
        return None
