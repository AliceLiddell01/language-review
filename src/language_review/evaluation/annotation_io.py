"""Слепой пакет JSONL + CSV; только явные человеческие решения становятся разметкой."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from language_review.errors import InputError
from language_review.evaluation.contracts import (
    MAX_BYTES,
    canonical,
    digest,
    jsonl_lines,
    read_json,
    save_new,
)
from language_review.evaluation.corpus import source_map, span, validate_corpus
from language_review.review_verify import _invalid_constant, _unique_objects

DECISIONS = (
    "id",
    "text_sha256",
    "text",
    "decision",
    "exhaustive",
    "categories",
    "terms",
    "exceptions",
    "note",
)
LABELS = ("id", "start", "end", "quote", "category", "replace", "replacement", "note")


def _records(corpus: dict[str, Any], split: str) -> list[dict[str, Any]]:
    maps = {doc["id"]: source_map(doc) for doc in corpus["documents"]}
    records = []
    for case in corpus["cases"]:
        if case["split"] != split:
            continue
        source = maps[case["document"]]
        start, end = span(source, case["range"])
        text = source.raw[start:end].decode(source.encoding)
        protected = []
        for location in case["protected_ranges"]:
            a, b = span(source, location)
            protected.append(
                [
                    len(source.raw[start:a].decode(source.encoding)),
                    len(source.raw[start:b].decode(source.encoding)),
                ]
            )
        records.append(
            {
                "id": case["id"],
                "text": text,
                "protected": protected,
                "offset_unit": "unicode_codepoints",
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    if not records:
        raise InputError("В выбранном split нет случаев для разметки.")
    return records


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export_annotations(corpus: dict[str, Any], split: str, destination: Path) -> None:
    records = _records(corpus, split)
    destination.mkdir(parents=True, exist_ok=False)
    # Никаких labels, corrections, вердиктов, terms, exceptions или evidence из gold.
    with (destination / "fragments.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    save_new(
        destination / "binding.json",
        {"corpus_sha256": digest(corpus), "split": split, "fragments_sha256": digest(records)},
    )
    _write_csv(
        destination / "decisions.csv",
        DECISIONS,
        [
            {
                "id": record["id"],
                "text_sha256": record["text_sha256"],
                "text": record["text"],
                "decision": "unreviewed",
                "exhaustive": "no",
                "categories": "",
                "terms": "",
                "exceptions": "",
                "note": "",
            }
            for record in records
        ],
    )
    _write_csv(destination / "labels.csv", LABELS, [])


def _csv(path: Path, fields: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise InputError("CSV превышает предел размера.")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    try:
        if tuple(reader.fieldnames or ()) != fields:
            raise InputError("Заголовок CSV не соответствует пакету разметки.")
        rows = list(reader)
    except csv.Error as exc:
        raise InputError("Некорректный CSV или превышен предел размера поля.") from exc
    if any(set(row) != set(fields) or any(value is None for value in row.values()) for row in rows):
        raise InputError("Число колонок CSV не соответствует заголовку.")
    return rows


def import_annotations(
    corpus: dict[str, Any], packet: Path, actor: str, evidence: str, attest_human: bool
) -> dict[str, Any]:
    binding = read_json(packet / "binding.json")
    if set(binding) != {"corpus_sha256", "split", "fragments_sha256"}:
        raise InputError("Некорректная привязка слепого пакета.")
    if binding["corpus_sha256"] != digest(corpus):
        raise InputError("Пакет относится к другой версии корпуса.")
    records = _records(corpus, binding["split"])
    if binding["fragments_sha256"] != digest(records):
        raise InputError("Изменены единицы слепой разметки.")
    with (packet / "fragments.jsonl").open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if (
        len(raw) > MAX_BYTES
        or [
            json.loads(line, object_pairs_hook=_unique_objects, parse_constant=_invalid_constant)
            for line in jsonl_lines(raw.decode("utf-8"))
        ]
        != records
    ):
        raise InputError("Изменены исходные фрагменты слепого пакета.")
    decisions = _csv(packet / "decisions.csv", DECISIONS)
    labels = _csv(packet / "labels.csv", LABELS)
    by_id = {record["id"]: record for record in records}
    if len(decisions) != len(records) or {row["id"] for row in decisions} != set(by_id):
        raise InputError("Решения должны однократно покрывать все ID пакета.")
    if any(row["id"] not in by_id for row in labels):
        raise InputError("Метка с неизвестным ID пакета.")
    reviewed = [row for row in decisions if row["decision"] != "unreviewed"]
    if reviewed and (not attest_human or not actor.strip() or not evidence.strip()):
        raise InputError("Требуются явное свидетельство человека, actor и evidence.")
    # Freeze — неизменяемая версия. Новые человеческие решения требуют нового release.
    if reviewed and binding["split"] == "test" and corpus["split_policy"]["test_frozen"]:
        raise InputError("Разметьте новую незамороженную версию, затем freeze-test.")
    result = copy.deepcopy(corpus)
    cases = {case["id"]: case for case in result["cases"]}
    documents = {doc["id"]: doc for doc in result["documents"]}
    for row in decisions:
        record = by_id[row["id"]]
        case = cases[row["id"]]
        rows = [label for label in labels if label["id"] == row["id"]]
        if row["text_sha256"] != record["text_sha256"] or row["text"] != record["text"]:
            raise InputError("SHA-256 текста решения не совпадает с пакетом.")
        if row["decision"] == "unreviewed":
            if (
                rows
                or row["exhaustive"] != "no"
                or any(row[key] for key in ("categories", "terms", "exceptions", "note"))
            ):
                raise InputError("Непроверенный случай не должен содержать решения или метки.")
            continue
        if row["decision"] not in {"clean", "errors", "disputed"} or row["exhaustive"] not in {
            "yes",
            "no",
        }:
            raise InputError("Некорректный verdict/exhaustive в CSV.")
        categories = row["categories"].split()
        if (
            not categories
            or len(set(categories)) != len(categories)
            or not set(categories) <= set(case["categories"])
        ):
            raise InputError("Явно укажите уникальные категории в пределах области случая.")
        exhaustive = row["exhaustive"] == "yes"
        status = (
            "disputed"
            if row["decision"] == "disputed"
            else ("independently_verified" if exhaustive else "draft")
        )
        history = [
            *case["annotation"]["history"],
            {
                "revision": digest({"binding": binding, "decision": row, "labels": rows}),
                "actor": actor,
                "decision": canonical({"decision": row, "labels": rows}).strip(),
            },
        ]
        annotation = {
            "status": status,
            "actor": actor,
            "method": "human",
            "evidence": evidence,
            "history": history,
        }
        source = source_map(documents[case["document"]])
        start_byte, _ = span(source, case["range"])
        base = len(source.raw[source.position(0).byte_offset : start_byte].decode(source.encoding))
        imported = []
        for index, label in enumerate(rows, 1):
            try:
                start, end = int(label["start"]), int(label["end"])
            except ValueError as exc:
                raise InputError("Offsets должны быть целыми Unicode codepoints.") from exc
            if (
                not 0 <= start <= end <= len(record["text"])
                or record["text"][start:end] != label["quote"]
            ):
                raise InputError("Offset/quote метки не совпадает с исходным текстом.")
            if label["category"] not in categories or label["replace"] not in {"yes", "no"}:
                raise InputError("Некорректная категория или replace метки.")
            if label["replace"] == "no" and label["replacement"]:
                raise InputError("Replacement требует replace=yes.")
            if any(
                start < b and a < end or start == end and a <= start <= b
                for a, b in record["protected"]
            ):
                raise InputError("Метка затрагивает защищённый технический диапазон.")
            location = source.range_dict(base + start, base + end)
            imported.append(
                {
                    "id": f"human-{index}",
                    "category": label["category"],
                    "anchors": [location],
                    "context": label["note"] or row["note"] or evidence,
                    "corrections": [{"range": location, "text": label["replacement"]}]
                    if label["replace"] == "yes"
                    else [],
                    "annotation": annotation,
                }
            )
        case.update(
            {
                "categories": categories,
                "terms": row["terms"].splitlines(),
                "exceptions": row["exceptions"].splitlines(),
                "exhaustive": exhaustive,
                "verdict": row["decision"],
                "annotation": annotation,
                "labels": imported,
            }
        )
    if reviewed:
        result["annotation_version"] = digest(
            {
                "previous": corpus["annotation_version"],
                "decisions": decisions,
                "labels": labels,
                "actor": actor,
                "evidence": evidence,
            }
        )
    return validate_corpus(result)
