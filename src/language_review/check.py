"""Конвейер `check`: фрагменты extract → движки → проверенные и согласованные замечания."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

from language_review.check_models import (
    Candidate,
    CheckDocument,
    CheckResult,
    CheckUnit,
    Engine,
    EngineRun,
    Finding,
    RawFinding,
    RejectedOutput,
    Suggestion,
    UnitOutcome,
)
from language_review.config import CheckSettings, LoadedConfig
from language_review.coordinates import position_within
from language_review.engine_languagetool import LanguageToolEngine
from language_review.engine_llm import LlmEngine
from language_review.engine_vale import ValeEngine
from language_review.extract_models import ExtractDocument, ResolvedFragment
from language_review.extract_report import fragment_id
from language_review.extraction import extract_paths
from language_review.masking import EngineView, ViewFragment, build_view

# Порядок запуска: быстрые детерминированные движки до LLM.
ENGINE_ORDER = ("languagetool", "vale", "llm")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_GROUPED_CONTEXTS = {"markdown:prose", "text:line", "restructured_text:prose", "asciidoc:prose"}
_GROUPED_KINDS = {"comment", "documentation_comment"}
_SENTENCE_CONTEXTS = {
    "markdown:prose",
    "restructured_text:prose",
    "asciidoc:prose",
    "asciidoc:heading",
}
# Категории, которые разные движки назначают одной и той же ошибке («не не» — повтор слова
# у Vale, опечатка у модели). Совместимость сама по себе не доказывает общий дефект.
CATEGORY_GROUPS = {
    "spelling": "language",
    "grammar": "language",
    "punctuation": "punctuation",
    "typography": "punctuation",
    "style": "style",
    "bureaucratese": "style",
    "terminology": "wording",
    "untranslated_english": "wording",
}
MAX_GROUP_FRAGMENTS = 40
MAX_GROUP_CHARS = 4000
NEGATION_WORDS = {
    "не",
    "ни",
    "нет",
    "нельзя",
    "без",
    "никогда",
    "ничего",
    "никто",
    "нигде",
    "никак",
    "ничто",
    "нисколько",
}
_WORD = re.compile(r"[^\W\d_]+")
# Слово английской прозы: латиница со строчной буквой, допустимы дефис и апостроф внутри,
# скобки, кавычки и знаки препинания по краям. Пути, флаги, вызовы и ВЕРХНИЙ РЕГИСТР не слова.
_ENGLISH_WORD = re.compile(
    r"""[("«]?(?=[A-Za-z'’-]*[a-z])[A-Za-z][A-Za-z'’-]*[.,;:!?…)"»]*""",
)
_LLM_PLACEHOLDER = re.compile(r"⟦\d+⟧")
# Виды фрагментов с человекочитаемым текстом; значения конфигурации — часто идентификаторы.
_PROSE_KINDS = {
    "comment",
    "documentation_comment",
    "user_facing_string",
    "markdown_prose",
    "plain_text",
}
# Меньше трёх слов — имя, команда или метка, а не английское предложение.
MIN_ENGLISH_WORDS = 3
_TECHNICAL = re.compile(r"\d+|[A-Za-z][A-Za-z0-9_]*")
# Символы, вставка которых в исходник требует экранирования или меняет разметку.
_SYNTAX_SENSITIVE = {
    "user_facing_string": set("\"'\\{}%$`"),
    "configured_value": set("\"'\\{}%$`<>&"),
    "markdown_prose": set("*_[]<>`\\|#"),
    "plain_text": set(),
}
EngineFactory = Callable[[str, CheckSettings], Engine]


def default_engine_factory(name: str, settings: CheckSettings) -> Engine:
    if name == "languagetool":
        return LanguageToolEngine(settings)
    if name == "vale":
        return ValeEngine(settings)
    if name == "llm":
        return LlmEngine(settings)
    raise ValueError(f"Неизвестный движок: {name}")


def public_source(label: str) -> str:
    """Происхождение значения без путей к конфигурации пользователя: отчёт можно передавать."""
    parts = []
    for part in label.split("; "):
        if part.startswith("конфигурация "):
            parts.append("файл конфигурации")
        elif part.startswith("профиль ") and " из " in part:
            parts.append(part.split(" из ", 1)[0])
        else:
            parts.append(part)
    return "; ".join(dict.fromkeys(parts))


def public_configuration(settings: CheckSettings, sources: dict[str, str]) -> dict[str, Any]:
    """Применённые фильтры проверки и их происхождение; сами термины в отчёт не попадают."""
    return {
        "engines": list(settings.engines),
        "categories": list(settings.categories),
        "terms_count": len(settings.terms),
        "sources": {
            key: public_source(sources.get(key, "по умолчанию"))
            for key in ("engines", "categories", "terms")
        },
    }


def _sentence_start(context: str) -> bool:
    return context in _SENTENCE_CONTEXTS or bool(re.fullmatch(r"markdown:h[1-6]", context))


def _groupable(previous: ResolvedFragment, fragment: ResolvedFragment) -> bool:
    first, second = previous.draft, fragment.draft
    if first.kind != second.kind or first.context != second.context:
        return False
    if first.kind not in _GROUPED_KINDS and first.context not in _GROUPED_CONTEXTS:
        return False
    if fragment.range["start"]["line"] != previous.range["end"]["line"] + 1:
        return False
    if first.kind in _GROUPED_KINDS:
        return bool(fragment.range["start"]["column"] == previous.range["start"]["column"])
    return True


def _fragment_groups(document: ExtractDocument) -> list[list[ResolvedFragment]]:
    groups: list[list[ResolvedFragment]] = []
    for fragment in document.fragments:
        group = groups[-1] if groups else None
        if (
            group is not None
            and len(group) < MAX_GROUP_FRAGMENTS
            and sum(len(item.text) for item in group) + len(fragment.text) <= MAX_GROUP_CHARS
            and _groupable(group[-1], fragment)
        ):
            group.append(fragment)
        else:
            groups.append([fragment])
    return groups


def _english_prose(unit: CheckUnit) -> bool:
    """Единица — английский человекочитаемый текст, а не имя, команда или защищённая вставка."""
    if unit.kind not in _PROSE_KINDS:
        return False
    text = unit.views["llm"].text
    placeholders = len(_LLM_PLACEHOLDER.findall(text))
    words = [
        token for token in _LLM_PLACEHOLDER.sub(" ", text).split() if _ENGLISH_WORD.fullmatch(token)
    ]
    # Больше защищённых вставок, чем слов, — код или перечень идентификаторов, а не проза.
    return len(words) >= MIN_ENGLISH_WORDS and placeholders <= len(words)


def _skip_reasons(unit: CheckUnit, settings: CheckSettings) -> dict[str, str]:
    """Применимость движков: правила LanguageTool и Vale русские, остаточный английский — у LLM."""
    if unit.has_russian:
        return {}
    reasons = dict.fromkeys(unit.engines, "no_russian_text")
    if "llm" in unit.engines:
        if settings.categories and "untranslated_english" not in settings.categories:
            reasons["llm"] = "category_disabled"
        elif _english_prose(unit):
            del reasons["llm"]
    return reasons


def build_units(
    documents: Sequence[ExtractDocument], settings: Sequence[CheckSettings]
) -> list[CheckUnit]:
    units: list[CheckUnit] = []
    for document_index, document in enumerate(documents):
        for group in _fragment_groups(document):
            view_fragments = [
                ViewFragment(item.start_char, item.end_char, item.text, item.draft.protected)
                for item in group
            ]
            views = {
                "plain": build_view(view_fragments, "plain"),
                "llm": build_view(view_fragments, "llm"),
            }
            unit = CheckUnit(
                index=len(units),
                document_index=document_index,
                document=document,
                fragments=tuple(group),
                fragment_ids=tuple(fragment_id(document.id, item.draft) for item in group),
                kind=group[0].draft.kind,
                context=group[0].draft.context,
                sentence_start=_sentence_start(group[0].draft.context),
                views=views,
                engines=settings[document_index].engines,
                terms=settings[document_index].terms,
            )
            unit.has_russian = bool(_CYRILLIC.search(views["plain"].text))
            unit.skip_reasons = _skip_reasons(unit, settings[document_index])
            units.append(unit)
    return units


def _fragment_at(unit: CheckUnit, offset: int) -> ResolvedFragment | None:
    for fragment in unit.fragments:
        if fragment.start_char <= offset <= fragment.end_char:
            return fragment
    return None


def source_position(unit: CheckUnit, offset: int) -> dict[str, int]:
    fragment = _fragment_at(unit, offset)
    if fragment is None:
        raise ValueError("Смещение вне фрагментов единицы проверки.")
    return position_within(
        fragment.range["start"],
        fragment.range["end"],
        fragment.text,
        unit.document.encoding or "utf-8",
        offset - fragment.start_char,
    )


def source_excerpt(unit: CheckUnit, start: int, end: int) -> str | None:
    """Точная цитата исходника, если диапазон лежит внутри одного фрагмента."""
    for fragment in unit.fragments:
        if fragment.start_char <= start and end <= fragment.end_char:
            return fragment.text[start - fragment.start_char : end - fragment.start_char]
    return None


def fragments_in_range(unit: CheckUnit, start: int, end: int) -> list[str]:
    return [
        identifier
        for identifier, fragment in zip(unit.fragment_ids, unit.fragments, strict=True)
        if fragment.start_char < end and start < fragment.end_char
    ]


def _negation_profile(text: str) -> tuple[int, int]:
    words = [word.casefold() for word in _WORD.findall(text)]
    standalone = sum(word in NEGATION_WORDS for word in words)
    prefixed = sum(
        len(word) > 3 and word.startswith(("не", "ни")) and word not in NEGATION_WORDS
        for word in words
    )
    return standalone, prefixed


_ENGLISH_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nothing|nobody|nowhere|neither|nor|without|cannot)\b|n't\b",
    re.IGNORECASE,
)
_ENGLISH_NEGATION_RUSSIAN = {"не", "нет", "нельзя", "без", "никогда", "ничего", "никто", "ни"}


def _english_technical(text: str) -> Counter[str]:
    """Акронимы, числа, snake_case и camelCase: перевод обязан сохранить их дословно."""
    return Counter(
        token
        for token in _TECHNICAL.findall(text)
        if token.isdigit()
        or (len(token) > 1 and token.isupper())
        or "_" in token
        or re.search(r"[a-z][A-Z]", token)
    )


def _english_translation_risks(original: str, replacement: str) -> list[str]:
    """Проверки перевода целого английского фрагмента на русский."""
    reasons: list[str] = []
    russian = {word.casefold() for word in _WORD.findall(replacement)} & _ENGLISH_NEGATION_RUSSIAN
    if bool(_ENGLISH_NEGATION.search(original)) != bool(russian):
        reasons.append("negation_changed")
    if _english_technical(original) - _english_technical(replacement):
        reasons.append("technical_content_changed")
    return reasons


def suggestion_risks(
    original: str, replacement: str, category: str, unit: CheckUnit, inserted: str
) -> list[str]:
    """Причины, по которым предложенная замена не может считаться пригодной кандидатурой."""
    if category == "untranslated_english" and not unit.has_russian:
        reasons = _english_translation_risks(original, replacement)
    else:
        reasons = []
        if _negation_profile(original) != _negation_profile(replacement):
            reasons.append("negation_changed")
        original_tokens = Counter(_TECHNICAL.findall(original))
        replacement_tokens = Counter(_TECHNICAL.findall(replacement))
        if category == "untranslated_english":
            original_tokens = Counter(
                token for token in original_tokens.elements() if token.isdigit()
            )
            replacement_tokens = Counter(
                token for token in replacement_tokens.elements() if token.isdigit()
            )
        if original_tokens != replacement_tokens:
            reasons.append("technical_content_changed")
    sensitive = _SYNTAX_SENSITIVE.get(unit.kind)
    if sensitive is None:
        sensitive = (
            set("<>&") if ":" in unit.context and unit.kind == "documentation_comment" else set()
        )
    if any(character in sensitive for character in inserted) or "*/" in inserted:
        reasons.append("requires_source_escaping")
    return reasons


def map_finding(raw: RawFinding, unit: CheckUnit) -> Candidate | RejectedOutput:
    view: EngineView = unit.views[raw.view]

    def reject(reason: str) -> RejectedOutput:
        return RejectedOutput(
            raw.engine, unit.index, reason, raw.rule_id, raw.message, raw.engine_excerpt, raw
        )

    if not 0 <= raw.start < raw.end <= len(view.text):
        return reject("position_out_of_range")
    if raw.engine_excerpt is not None and view.text[raw.start : raw.end] != raw.engine_excerpt:
        return reject("position_mismatch")
    if view.only_masked(raw.start, raw.end):
        return reject("targets_protected_text")
    mapped = view.source_range(raw.start, raw.end)
    if mapped is None:
        return reject("range_boundary_in_protected_text")
    suggestions: list[Suggestion] = []
    original = view.text[raw.start : raw.end]
    for replacement in raw.replacements:
        edit = view.map_edit(raw.start, raw.end, replacement)
        if isinstance(edit, str):
            suggestions.append(
                Suggestion(
                    [raw.engine], raw.replacement_origin, "rejected", [edit], replacement, None
                )
            )
            continue
        reasons = suggestion_risks(original, replacement, raw.category, unit, edit.text)
        if source_excerpt(unit, edit.source_start, edit.source_end) is None:
            reasons.append("replacement_spans_fragments")
        suggestions.append(
            Suggestion(
                [raw.engine],
                raw.replacement_origin,
                "rejected" if reasons else "candidate",
                reasons,
                replacement,
                (edit.source_start, edit.source_end, edit.text),
            )
        )
    return Candidate(raw, unit, mapped[0], mapped[1], suggestions)


def _finding_id(document: ExtractDocument, finding: Finding) -> str:
    payload = json.dumps(
        {
            "document": document.id,
            "start": finding.source_start,
            "end": finding.source_end,
            "category": finding.category,
            "evidence": sorted(_raw_key(item.raw) for item in finding.candidates),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "find-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _raw_key(raw: RawFinding) -> str:
    return json.dumps(asdict(raw), ensure_ascii=False, sort_keys=True)


def _compatible(first: str, second: str) -> bool:
    return first == second or CATEGORY_GROUPS.get(first, first) == CATEGORY_GROUPS.get(
        second, second
    )


def _cluster_category(members: list[Candidate]) -> str:
    """Самая частая категория группы; при равенстве — по порядку детерминированных движков."""
    counts = Counter(item.raw.category for item in members)
    return min(
        counts,
        key=lambda category: (
            -counts[category],
            min(
                ENGINE_ORDER.index(item.raw.engine)
                for item in members
                if item.raw.category == category
            ),
            category,
        ),
    )


def _same_defect(first: Candidate, second: Candidate) -> bool:
    """Общая минимальная правка — свидетельство общей гипотезы, не её правильности."""
    if first.unit is not second.unit or not _compatible(first.raw.category, second.raw.category):
        return False
    if first.raw == second.raw:
        return True
    # Разные правила одного движка остаются самостоятельными диагнозами.
    if first.raw.engine == second.raw.engine and first.raw.rule_id != second.raw.rule_id:
        return False
    edits = {item.edit for item in first.suggestions if item.edit is not None}
    return bool(edits & {item.edit for item in second.suggestions if item.edit is not None})


def reconcile(document_index: int, candidates: list[Candidate]) -> list[Finding]:
    """Согласовать по общей правке всей группы, сохраняя независимые диагнозы."""
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.source_start,
            item.source_end,
            ENGINE_ORDER.index(item.raw.engine) if item.raw.engine in ENGINE_ORDER else 99,
            item.raw.rule_id or "",
            item.raw.category,
            item.raw.message,
            item.raw.replacements,
            item.raw.confidence or "",
            item.raw.severity or "",
            item.raw.view,
            item.raw.start,
            item.raw.end,
            item.raw.rule_category or "",
            item.raw.engine_excerpt or "",
            item.raw.replacement_origin,
            _raw_key(item.raw),
        ),
    )
    clusters: list[list[Candidate]] = []
    shared_edits: list[set[tuple[int, int, str]]] = []
    by_edit: dict[tuple[int, tuple[int, int, str]], set[int]] = {}
    by_rule: dict[tuple[str, str | None], set[int]] = {}
    without_engine: dict[str, set[int]] = {item.raw.engine: set() for item in ordered}
    seen: set[tuple[int, RawFinding]] = set()
    for candidate in ordered:
        identity = (id(candidate.unit), candidate.raw)
        if identity in seen:
            continue
        seen.add(identity)
        edits = {item.edit for item in candidate.suggestions if item.edit is not None}
        possible: set[int] = set()
        engine, rule = candidate.raw.engine, candidate.raw.rule_id
        for edit in edits:
            groups = by_edit.get((id(candidate.unit), edit), set())
            possible.update(groups.intersection(by_rule.get((engine, rule), ())))
            possible.update(groups.intersection(without_engine[engine]))
        index = next(
            (
                index
                for index in sorted(possible)
                if all(_same_defect(item, candidate) for item in clusters[index])
            ),
            None,
        )
        if index is None:
            index = len(clusters)
            clusters.append([candidate])
            shared_edits.append(edits)
            for name, groups in without_engine.items():
                if name != engine:
                    groups.add(index)
            for edit in edits:
                by_edit.setdefault((id(candidate.unit), edit), set()).add(index)
        else:
            clusters[index].append(candidate)
            for edit in shared_edits[index] - edits:
                by_edit[(id(candidate.unit), edit)].remove(index)
            shared_edits[index].intersection_update(edits)
        without_engine[engine].discard(index)
        by_rule.setdefault((engine, rule), set()).add(index)
    findings: list[Finding] = []
    for members in clusters:
        suggestions: list[Suggestion] = []
        for candidate in members:
            for suggestion in candidate.suggestions:
                same = next(
                    (
                        item
                        for item in suggestions
                        if item.edit is not None
                        and item.edit == suggestion.edit
                        and item.status == suggestion.status
                    ),
                    None,
                )
                if same is not None:
                    same.engines = sorted(set(same.engines) | set(suggestion.engines))
                else:
                    suggestions.append(
                        Suggestion(
                            list(suggestion.engines),
                            suggestion.origin,
                            suggestion.status,
                            list(suggestion.reasons),
                            suggestion.engine_replacement,
                            suggestion.edit,
                        )
                    )
        finding = Finding(
            id="",
            document_index=document_index,
            category=_cluster_category(members),
            source_start=min(item.source_start for item in members),
            source_end=max(item.source_end for item in members),
            candidates=members,
            suggestions=suggestions,
        )
        finding.id = _finding_id(members[0].unit.document, finding)
        findings.append(finding)
    findings.sort(key=lambda item: (item.source_start, item.source_end, item.category, item.id))
    return findings


def _engine_status(run: EngineRun) -> str:
    if run.status == "unavailable":
        return "unavailable"
    if run.units_failed == 0 and run.units_skipped == 0:
        return "ok"
    if run.units_checked == 0 and run.units_failed > 0:
        return "error"
    return "partial"


def _document_status(document: CheckDocument) -> str:
    extract_status = document.extract.status
    if extract_status in {"error", "skipped"}:
        return extract_status
    pairs = [
        (unit, engine)
        for unit in document.units
        for engine in document.engines
        if unit.checked_by(engine)
    ]
    if extract_status == "unsupported" and not document.extract.fragments:
        return "unsupported"
    statuses = [
        document.unit_status.get((unit.index, engine), ("missing", None))[0]
        for unit, engine in pairs
    ]
    incomplete = any(status != "checked" for status in statuses)
    checked_any = "checked" in statuses
    failed_any = any(status not in {"checked", "skipped"} for status in statuses)
    if pairs and failed_any and not checked_any:
        return "unavailable"
    if incomplete or extract_status in {"partial", "unsupported"}:
        return "partial"
    return "findings" if document.findings else "clean"


def overall_status(
    result_documents: list[CheckDocument], engines: list[EngineRun], input_error: bool
) -> str:
    if input_error:
        return "error"
    if any(run.status in {"unavailable", "error"} for run in engines):
        return "unavailable"
    if any(run.status == "partial" for run in engines) or any(
        document.status in {"partial", "error", "unsupported", "unavailable"}
        for document in result_documents
    ):
        return "partial"
    if any(document.findings for document in result_documents):
        return "findings"
    return "clean"


def check_paths(
    paths: list[str],
    config: LoadedConfig,
    *,
    requested_profile: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    check_overrides: dict[str, Any] | None = None,
    engine_factory: EngineFactory = default_engine_factory,
) -> CheckResult:
    """Проверить тексты, извлечённые `extract`, выбранными движками без изменения исходников."""
    base_settings, base_sources = config.check_settings_for_profile(None, check_overrides)
    extract = extract_paths(
        paths, config, requested_profile=requested_profile, cli_overrides=cli_overrides
    )
    effective = [
        config.check_settings_for_profile(document.profile, check_overrides)
        for document in extract.documents
    ]
    settings = [item[0] for item in effective]
    units = build_units(extract.documents, settings)
    documents = [
        CheckDocument(
            extract=document,
            status="",
            engines=settings[index].engines,
            configuration=public_configuration(*effective[index]),
        )
        for index, document in enumerate(extract.documents)
    ]
    for unit in units:
        documents[unit.document_index].units.append(unit)

    # Профиль документа может сузить набор движков; без документов действует базовый набор.
    selected = {name for item in settings for name in item.engines} or set(base_settings.engines)
    engine_names = [name for name in ENGINE_ORDER if name in selected]
    runs: list[EngineRun] = []
    candidates: dict[int, list[Candidate]] = {}
    for name in engine_names:
        engine = engine_factory(name, base_settings)
        run = EngineRun(name=name, details=engine.describe())
        runs.append(run)
        engine_units = [unit for unit in units if unit.checked_by(name)]
        run.units_total = len(engine_units)
        started = time.perf_counter()
        try:
            probe = engine.probe()
            run.details.update(probe.details)
            run.diagnostics.extend(probe.diagnostics)
            run.message = probe.message
            outcomes: list[UnitOutcome]
            if probe.status != "available":
                run.status = "unavailable"
                outcomes = [
                    UnitOutcome(unit.index, "failed", "engine_unavailable") for unit in engine_units
                ]
            else:
                outcomes = list(engine.run(engine_units, run))
        finally:
            engine.close()
        run.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        by_index = {unit.index: unit for unit in engine_units}
        reported = set()
        for outcome in outcomes:
            reported.add(outcome.unit)
            _record_outcome(
                outcome, by_index[outcome.unit], name, run, documents, candidates, settings
            )
        for unit in engine_units:
            if unit.index not in reported:
                _record_outcome(
                    UnitOutcome(unit.index, "failed", "engine_missing_result"),
                    unit,
                    name,
                    run,
                    documents,
                    candidates,
                    settings,
                )
        run.status = _engine_status(run)
        failure_reasons = sorted(
            {
                reason
                for document in documents
                for (_unit, engine_name), (status, reason) in document.unit_status.items()
                if engine_name == name and status != "checked" and reason
            }
        )
        run.diagnostics = sorted(set(run.diagnostics) | set(failure_reasons))

    for document_index, document in enumerate(documents):
        document.findings = reconcile(document_index, candidates.get(document_index, []))
        for unit in document.units:
            for engine_name, reason in unit.skip_reasons.items():
                document.unit_status[(unit.index, engine_name)] = ("skipped", reason)
        document.status = _document_status(document)
        document.diagnostics = sorted(set(document.extract.diagnostics))

    status = overall_status(documents, runs, bool(extract.scan_result.input_errors))
    return CheckResult(
        extract=extract,
        documents=documents,
        engines=runs,
        status=status,
        settings={
            "engines": list(base_settings.engines),
            "categories": list(base_settings.categories),
            "llm_provider": base_settings.llm_provider,
            "sources": {
                key: public_source(base_sources.get(key, "по умолчанию"))
                for key in ("engines", "categories", "llm.provider")
            },
        },
    )


def _record_outcome(
    outcome: UnitOutcome,
    unit: CheckUnit,
    engine: str,
    run: EngineRun,
    documents: list[CheckDocument],
    candidates: dict[int, list[Candidate]],
    settings: list[CheckSettings],
) -> None:
    document = documents[unit.document_index]
    document.unit_status[(unit.index, engine)] = (outcome.status, outcome.reason)
    if outcome.status == "checked":
        run.units_checked += 1
        run.fragments_checked += len(unit.fragments)
    elif outcome.status == "skipped":
        run.units_skipped += 1
    else:
        run.units_failed += 1
    document.rejected.extend(outcome.rejected)
    run.rejected += len(outcome.rejected)
    categories = settings[unit.document_index].categories
    for raw in outcome.findings:
        if categories and raw.category not in categories:
            run.filtered += 1
            document.filtered.append(raw)
            continue
        mapped = map_finding(raw, unit)
        if isinstance(mapped, RejectedOutput):
            document.rejected.append(mapped)
            run.rejected += 1
            continue
        run.findings += 1
        candidates.setdefault(unit.document_index, []).append(mapped)
