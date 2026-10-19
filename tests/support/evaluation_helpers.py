"""Синтетическая разметка исключительно для проверки оценщика, не baseline русского языка."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from language_review.coordinates import SourceMap
from language_review.evaluation.corpus import validate_corpus


def annotation(actor: str = "contract-author") -> dict[str, Any]:
    return {
        "status": "independently_verified",
        "actor": actor,
        "method": "synthetic_contract",
        "evidence": "Рукотворный контроль арифметики и контрактов; не независимая языковая оценка.",
        "history": [{"revision": "1", "actor": actor, "decision": "synthetic fixture"}],
    }


def make_corpus(
    text: str = "Первая ошыбка. Вторая ашибка. Важные файл. Протокол работает.",
    path: str = "sample.md",
    encoding: str = "utf-8",
    bom: str | None = None,
) -> dict[str, Any]:
    prefix = {"utf-8": b"\xef\xbb\xbf", "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(
        bom or "", b""
    )
    raw = prefix + text.encode(encoding)
    source = SourceMap.create(raw, encoding, bom)
    return {
        "schema": "language-review.corpus",
        "schema_version": "1.0",
        "id": "synthetic-contract",
        "version": "1",
        "annotation_version": "1",
        "split_policy": {
            "strategy": "repository grouping; normalized shingles/containment; manual audit",
            "test_frozen": False,
            "test_sha256": None,
            "holdout_policy": "No answers in prompts or retrieval",
        },
        "documents": [
            {
                "id": "doc-1",
                "repository": "demo",
                "path": path,
                "revision": "fixture-1",
                "origin": "Original synthetic material created for Language Review contracts",
                "rights": {
                    "use": "permitted",
                    "publish": "permitted",
                    "basis": "Project-owned synthetic fixture",
                },
                "data_kind": "synthetic",
                "content_base64": base64.b64encode(raw).decode("ascii"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "encoding": encoding,
                "bom": bom,
            }
        ],
        "cases": [
            {
                "id": "case-1",
                "document": "doc-1",
                "split": "dev",
                "range": source.range_dict(0, len(text)),
                "context": "documentation",
                "profile": None,
                "languages": ["ru"],
                "categories": ["spelling", "grammar"],
                "terms": ["CLI"],
                "exceptions": [],
                "protected_ranges": [],
                "applicable": True,
                "exhaustive": True,
                "verdict": "clean",
                "annotation": annotation(),
                "labels": [],
            }
        ],
    }


def add_label(
    corpus: dict[str, Any],
    excerpt: str,
    category: str = "spelling",
    corrections: tuple[str, ...] = (),
    occurrence: int = 0,
) -> dict[str, Any]:
    from language_review.evaluation.corpus import source_map

    source = source_map(corpus["documents"][0])
    start = -1
    for _ in range(occurrence + 1):
        start = source.text.index(excerpt, start + 1)
    location = source.range_dict(start, start + len(excerpt))
    label = {
        "id": f"gold-{len(corpus['cases'][0]['labels']) + 1}",
        "category": category,
        "anchors": [location],
        "context": "Synthetic controlled diagnosis",
        "corrections": [{"range": location, "text": text} for text in corrections],
        "annotation": annotation(),
    }
    corpus["cases"][0]["labels"].append(label)
    corpus["cases"][0]["verdict"] = "errors"
    return label


def write_source(corpus: dict[str, Any], root: Path) -> Path:
    validate_corpus(corpus)
    document = corpus["documents"][0]
    root.mkdir(parents=True, exist_ok=True)
    path = root / document["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(document["content_base64"]))
    return path
