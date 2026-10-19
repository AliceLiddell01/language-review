"""Переносимый корпус с неизменяемыми байтами и независимым происхождением меток."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata
from itertools import combinations
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.evaluation.contracts import digest, read_json, validate
from language_review.review_verify import _verify_quote
from language_review.scanner import _classify_encoding


def safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or any(part in {"..", "."} or part.endswith((" ", ".")) for part in path.parts)
        or any(char in value for char in '\\:<>"|?*\x00')
        or any(ord(char) < 32 for char in value)
        or any(
            part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)),
            }
            for part in path.parts
        )
    ):
        raise InputError("Небезопасный относительный путь корпуса.")
    return path


def source_map(document: dict[str, Any]) -> SourceMap:
    try:
        raw = base64.b64decode(document["content_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != document["sha256"]:
            raise ValueError("source_sha256_mismatch")
        encoding, bom, _, reason = _classify_encoding(raw)
        if reason or encoding != document["encoding"] or bom != document["bom"]:
            raise ValueError("scan_encoding_mismatch")
        return SourceMap.create(raw, document["encoding"], document["bom"])
    except (ValueError, LookupError, binascii.Error) as exc:
        raise InputError("Байты, SHA-256 или кодировка корпуса не согласованы.") from exc


def span(source: SourceMap, location: dict[str, Any]) -> tuple[int, int]:
    try:
        start = location["start"]["byte_offset"]
        end = location["end"]["byte_offset"]
        quote = source.raw[start:end].decode(source.encoding)
        _verify_quote(source, location, quote)
        return start, end
    except (ValueError, KeyError, TypeError, LookupError) as exc:
        raise InputError("Недостоверные координаты исходника.") from exc


def contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    return bool(
        outer["start"]["byte_offset"] <= inner["start"]["byte_offset"]
        and inner["end"]["byte_offset"] <= outer["end"]["byte_offset"]
    )


def overlaps(first: dict[str, Any], second: dict[str, Any]) -> bool:
    a, b = (first[key]["byte_offset"] for key in ("start", "end"))
    c, d = (second[key]["byte_offset"] for key in ("start", "end"))
    return bool((a < d and c < b) or (a == b and c <= a <= d) or (c == d and a <= c <= b))


def _verified(annotation: dict[str, Any], kind: str) -> None:
    if annotation["status"] == "independently_verified":
        if kind == "real" and annotation["method"] not in {"human", "external_reference"}:
            raise InputError("Реальная эталонная метка требует независимого подтверждения.")
        if not annotation["evidence"] or not annotation["history"]:
            raise InputError("Подтверждённая метка требует основания и истории.")


def test_hash(corpus: dict[str, Any]) -> str:
    cases = [case for case in corpus["cases"] if case["split"] == "test"]
    identifiers = {case["document"] for case in cases}
    return digest(
        {
            "cases": cases,
            "documents": [doc for doc in corpus["documents"] if doc["id"] in identifiers],
        }
    )


def validate_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    validate(corpus, "corpus")
    try:
        documents = {doc["id"]: doc for doc in corpus["documents"]}
        if len(documents) != len(corpus["documents"]):
            raise InputError("Повторный ID документа.")
        identities = [
            (doc["repository"].casefold(), doc["path"].casefold()) for doc in documents.values()
        ]
        if len(set(identities)) != len(identities):
            raise InputError("Повторный путь документа.")
        maps = {key: source_map(doc) for key, doc in documents.items()}
        for doc in documents.values():
            if "provenance" in doc and doc["provenance"]["source_revision"] != doc["revision"]:
                raise InputError("Revision документа отличается от pinned provenance.")
            safe_path(doc["path"])
            safe_path(doc["repository"])
            if "/" in doc["repository"]:
                raise InputError("repository должен быть одним переносимым именем.")
        seen: set[str] = set()
        by_document: dict[str, list[dict[str, Any]]] = {key: [] for key in documents}
        for case in corpus["cases"]:
            if case["id"] in seen or case["document"] not in documents:
                raise InputError("Повторный case ID или неизвестный документ.")
            seen.add(case["id"])
            document = documents[case["document"]]
            if (
                "provenance" in document
                and case["split"] != document["provenance"]["upstream_split"]
            ):
                raise InputError("Нельзя переносить pinned источник в другой split.")
            source = maps[case["document"]]
            span(source, case["range"])
            _verified(case["annotation"], document["data_kind"])
            for other in by_document[case["document"]]:
                if overlaps(case["range"], other["range"]):
                    raise InputError("Оценочные случаи одного файла не должны пересекаться.")
            by_document[case["document"]].append(case)
            labels: set[str] = set()
            for label in case["labels"]:
                if label["category"] not in case["categories"]:
                    raise InputError("Категория метки должна входить в область разметки случая.")
                if label["id"] in labels:
                    raise InputError("Повторный label ID.")
                labels.add(label["id"])
                _verified(label["annotation"], document["data_kind"])
                for location in label["anchors"]:
                    span(source, location)
                    if not contains(case["range"], location):
                        raise InputError("Метка вне оценочного случая.")
                for correction in label["corrections"]:
                    span(source, correction["range"])
                    if not contains(case["range"], correction["range"]):
                        raise InputError("Исправление вне оценочного случая.")
            for location in case["protected_ranges"]:
                span(source, location)
                if not contains(case["range"], location):
                    raise InputError("Защищённый диапазон вне оценочного случая.")
            if case["verdict"] == "clean" and case["labels"]:
                raise InputError("Чистый случай не может содержать метки ошибок.")
            if case["verdict"] == "errors" and not case["labels"]:
                raise InputError("Случай errors требует меток.")
        # Нормализация зависит только от случая, а не от пары.
        case_texts: dict[str, tuple[str, set[str]]] = {}
        if len({case["split"] for case in corpus["cases"]}) > 1:
            for case in corpus["cases"]:
                source = maps[case["document"]]
                start, end = span(source, case["range"])
                text = unicodedata.normalize("NFKC", source.raw[start:end].decode(source.encoding))
                normalized = " ".join(re.findall(r"\w+", text.casefold()))
                case_texts[case["id"]] = (
                    normalized,
                    {normalized[i : i + 5] for i in range(max(0, len(normalized) - 4))},
                )
        # Консервативный запрет пересечения репозитория/файла/точных и близких текстов.
        for first, second in combinations(corpus["cases"], 2):
            if first["split"] == second["split"]:
                continue
            a, b = documents[first["document"]], documents[second["document"]]
            if a["repository"].casefold() == b["repository"].casefold():
                raise InputError("Утечка репозитория между split.")
            (left, left_shingles), (right, right_shingles) = (
                case_texts[first["id"]],
                case_texts[second["id"]],
            )
            union = left_shingles | right_shingles
            similarity = len(left_shingles & right_shingles) / len(union) if union else 0
            if (
                a["sha256"] == b["sha256"]
                or left == right
                or (
                    min(len(left), len(right)) >= 20
                    and (left in right or right in left or similarity >= 0.8)
                )
            ):
                raise InputError("Утечка дубликата/близкого текста между split.")
        policy = corpus["split_policy"]
        if policy["test_frozen"] and policy["test_sha256"] != test_hash(corpus):
            raise InputError("Изменён замороженный test split.")
        if not policy["test_frozen"] and policy["test_sha256"] is not None:
            raise InputError("Незамороженный test split не должен иметь freeze hash.")
        return corpus
    except (KeyError, ValueError, TypeError) as exc:
        raise InputError("Некорректные связи корпуса.") from exc


def load_corpus(path: Path) -> dict[str, Any]:
    return validate_corpus(read_json(path))


def eligible(case: dict[str, Any]) -> bool:
    return (
        case["annotation"]["status"] == "independently_verified"
        and case["exhaustive"]
        and case["applicable"]
        and case["verdict"] != "disputed"
        and all(
            label["annotation"]["status"] == "independently_verified" for label in case["labels"]
        )
    )


def annotation_agreement(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Точное согласие независимых аннотаций; без назначения истины большинством."""
    if first["documents"] != second["documents"]:
        raise InputError("Для согласованности нужны одинаковые неизменяемые документы.")
    left = {case["id"]: case for case in first["cases"]}
    right = {case["id"]: case for case in second["cases"]}
    pairs = sorted(left.keys() & right.keys())
    comparable, equal, conflicts = 0, 0, []
    excluded_methods = []
    for key in pairs:
        a, b = left[key], right[key]
        if a["range"] != b["range"] or a["document"] != b["document"]:
            raise InputError("Изменена единица аннотации.")
        if a["annotation"]["actor"] == b["annotation"]["actor"]:
            continue
        annotations = [
            a["annotation"],
            b["annotation"],
            *(label["annotation"] for case in (a, b) for label in case["labels"]),
        ]
        if any(value["method"] not in {"human", "external_reference"} for value in annotations):
            excluded_methods.append(key)
            continue

        def signature(case: dict[str, Any]) -> Any:
            return [
                case["verdict"],
                sorted(case["categories"]),
                case["exhaustive"],
                sorted(
                    digest({k: label[k] for k in ("category", "anchors", "corrections")})
                    for label in case["labels"]
                ),
            ]

        comparable += 1
        if signature(a) == signature(b):
            equal += 1
        else:
            conflicts.append(key)
    return {
        "paired_cases": comparable,
        "exact_agreements": equal,
        "agreement": equal / comparable if comparable else None,
        "conflicts": conflicts,
        "unpaired": sorted(left.keys() ^ right.keys()),
        "excluded_nonindependent_method": excluded_methods,
        "method": "exact-case-agreement; no chance correction; no automatic truth assignment",
    }
