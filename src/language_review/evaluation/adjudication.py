"""Явные независимые решения для спорных совпадений и смыслового ущерба правок."""

from __future__ import annotations

from typing import Any

from language_review.errors import InputError
from language_review.evaluation.contracts import digest, validate


def apply_judgments(
    judgments: dict[str, Any],
    corpus: dict[str, Any],
    report: dict[str, Any],
    rows: dict[str | None, list[dict[str, Any]]],
) -> None:
    validate(judgments, "judgments")
    if judgments["corpus_sha256"] != digest(corpus) or judgments["report_sha256"] != digest(report):
        raise InputError("Ручные решения относятся к другому корпусу или исходному отчёту.")
    seen = set()
    for entry in judgments["entries"]:
        annotation = entry["annotation"]
        if (
            annotation["status"] != "independently_verified"
            or annotation["method"] not in {"human", "external_reference"}
            or not annotation["history"]
            or not annotation["evidence"]
        ):
            raise InputError("Решение требует независимого подтверждения, основания и истории.")
        identity = (entry["engine"], entry["case"], entry["candidate"])
        if identity in seen:
            raise InputError("Повторное ручное решение кандидата.")
        seen.add(identity)
        case = next(
            (value for value in rows.get(entry["engine"], []) if value["id"] == entry["case"]), None
        )
        if case is None or not case["eligible"]:
            raise InputError("Неизвестный или не подтверждённый оценочный случай.")
        item = next(
            (value for value in case["candidates"] if value["key"] == entry["candidate"]), None
        )
        if item is None:
            raise InputError("Ручное решение не связано с кандидатом сохранённого отчёта.")
        if item["outcome"] == "not_evaluated":
            raise InputError("Сначала расширьте независимо размеченную область категорий корпуса.")
        if entry["detection"] == "tp":
            if entry["label"] not in {label["id"] for label in case["gold"]}:
                raise InputError("TP требует точную ссылку на эталонную метку.")
        elif entry["label"] is not None:
            raise InputError("Только TP может закрывать эталонную метку.")
        if entry["correction"] == "correct" and entry["detection"] != "tp":
            raise InputError("Правильная коррекция требует обнаруженной ошибки.")
        item.update(
            outcome=entry["detection"],
            label=entry["label"],
            correction=entry["correction"],
            harm=entry["harm"],
            decision_provenance="independent_judgment",
        )
    for values in rows.values():
        for case in values:
            tp = [item["label"] for item in case["candidates"] if item["outcome"] == "tp"]
            if len(set(tp)) != len(tp):
                raise InputError("Ручные решения нарушают взаимную однозначность matching.")
            case["tp"] = sorted(tp)
            # Неразрешённые автоматические связи не назначаются FN произвольно.
            unresolved = {
                label
                for item in case["candidates"]
                if item["outcome"] == "needs_adjudication"
                for label in item["potential_labels"]
            } - set(tp)
            case["coverage"]["unresolved_gold"] = sorted(unresolved)
            case["fn"] = sorted({label["id"] for label in case["gold"]} - set(tp) - set(unresolved))
            reasons = [
                reason for reason in case["reasons"] if reason != "matching:needs_adjudication"
            ]
            if any(
                item["outcome"] == "needs_adjudication"
                or item["correction"] == "needs_adjudication"
                for item in case["candidates"]
            ):
                reasons.append("matching:needs_adjudication")
            case["reasons"] = reasons
            if case["eligible"]:
                case["state"] = "incomplete" if reasons else "evaluated"
