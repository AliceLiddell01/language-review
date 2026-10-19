"""Разрешённые pinned источники. Внешние пары — предложения, а не gold labels."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any

from language_review.config import CHECK_CATEGORIES, ExtractSettings
from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.evaluation.contracts import MAX_BYTES, digest, save_new
from language_review.evaluation.corpus import safe_path, source_map, validate_corpus
from language_review.extract_code import extract_code
from language_review.extract_docs import extract_markdown
from language_review.review_verify import _invalid_constant, _unique_objects
from language_review.scanner import _classify_encoding


def recipes() -> dict[str, dict[str, Any]]:
    values = json.loads(resources.files(__package__).joinpath("sources.json").read_text("utf-8"))
    return {value["id"]: value for value in values}


def _validate_recipe(recipe: dict[str, Any]) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", recipe["revision"]):
        raise InputError("Источник требует неизменяемый commit SHA.")
    if recipe["license"] not in {"MIT", "BSD-3-Clause", "MIT-card/Apache-2.0-script"} or not all(
        recipe[key].strip() for key in ("rights_basis", "attribution")
    ):
        raise InputError("Не подтверждены лицензия, права или атрибуция источника.")
    if recipe["split"] != {"markdown": "dev", "pairs": "test", "code": "validation"}.get(
        recipe["kind"]
    ):
        raise InputError("Изменён предписанный split источника.")
    paths = [file["path"] for file in recipe["files"]]
    if len(set(paths)) != len(paths) or recipe["license_path"] not in paths:
        raise InputError("Нужны уникальные файлы и исходное основание прав.")
    for file in recipe["files"]:
        safe_path(file["path"])
        if not re.fullmatch(r"[a-f0-9]{64}", file["sha256"]):
            raise InputError("Файл источника требует SHA-256.")
    if recipe["base_url"] not in (
        (
            "https://raw.githubusercontent.com/rust-lang-ru/book",
            "https://huggingface.co/datasets/ai-forever/spellcheck_punctuation_benchmark/resolve",
            "https://raw.githubusercontent.com/emgyrz/cubic_spline",
            "https://raw.githubusercontent.com/X-rus/xNet",
            "https://raw.githubusercontent.com/FarGroup/FarManager",
            "https://raw.githubusercontent.com/by-sonic/tglock",
        )
    ):
        raise InputError("Источник отсутствует в разрешённом каталоге.")


def _url(recipe: dict[str, Any], path: str) -> str:
    return f"{recipe['base_url']}/{recipe['revision']}/{path}"


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = bytes(response.read(MAX_BYTES + 1))
        if len(raw) > MAX_BYTES:
            raise InputError("Источник превышает предел размера.")
        return raw
    except (OSError, urllib.error.URLError) as exc:
        raise InputError("Pinned источник недоступен; замена версии запрещена.") from exc


def draft_annotation(evidence: str) -> dict[str, Any]:
    return {
        "status": "draft",
        "actor": "unassigned",
        "method": "pipeline",
        "evidence": evidence,
        "history": [],
    }


def _document(recipe: dict[str, Any], path: str, raw: bytes, origin: str) -> dict[str, Any]:
    encoding, bom, _, reason = _classify_encoding(raw)
    if reason or not encoding:
        raise InputError("Байты источника не поддерживаются scan.")
    return {
        "id": digest({"source": recipe["id"], "path": path})[:24],
        "repository": recipe["id"],
        "path": path,
        "revision": recipe["revision"],
        "origin": origin,
        "rights": {
            "use": "permitted",
            "publish": "private_only",
            "basis": recipe["rights_basis"] + " Attribution: " + recipe["attribution"],
        },
        "data_kind": "real",
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": encoding,
        "bom": bom,
        "provenance": {
            "source_url": origin.split("#")[0],
            "source_revision": recipe["revision"],
            "source_sha256": next(
                file["sha256"]
                for file in recipe["files"]
                if file["path"]
                == ("data/GitHubTypoCorpusRu/test.json" if recipe["kind"] == "pairs" else path)
            ),
            "license": recipe["license"],
            "license_url": _url(recipe, recipe["license_path"]),
            "license_sha256": next(
                file["sha256"] for file in recipe["files"] if file["path"] == recipe["license_path"]
            ),
            "attribution": recipe["attribution"],
            "upstream_split": recipe["split"],
            "transformation": "original bytes"
            if recipe["kind"] != "pairs"
            else "UTF-8 encoding of exact JSONL source string; "
            "not original GitHub file; row in origin",
        },
    }


def _case(
    recipe: dict[str, Any],
    document: dict[str, Any],
    location: dict[str, Any],
    context: str,
    protected: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": digest({"document": document["id"], "range": location})[:24],
        "document": document["id"],
        "split": recipe["split"],
        "range": location,
        "context": context,
        "profile": None,
        "languages": ["ru"],
        "categories": list(CHECK_CATEGORIES),
        "terms": [],
        "exceptions": [],
        "protected_ranges": protected,
        "applicable": True,
        "exhaustive": False,
        "verdict": "disputed",
        "annotation": draft_annotation(document["origin"]),
        "labels": [],
    }


def pair_proposal(source: SourceMap, correction: str) -> dict[str, Any]:
    """Только единственный минимальный single-splice. Даже он остаётся черновиком.

    Не выбирает один из неоднозначных anchors повторяющихся символов; внутренние
    совпадения широкого diff требуют человека. Это не классификатор ошибок.
    """
    text = source.text
    if max(len(text), len(correction)) > 20000:
        return {"status": "size_limit_requires_human", "edits": []}
    if text == correction:
        return {"status": "unchanged_not_attested_clean", "edits": []}
    prefix = 0
    while prefix < min(len(text), len(correction)) and text[prefix] == correction[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(text), len(correction)) - prefix
        and text[-suffix - 1] == correction[-suffix - 1]
    ):
        suffix += 1
    end = len(text) - suffix
    replacement = correction[prefix : len(correction) - suffix]
    # Alternative equally short splices (e.g. delete one of repeated letters).
    width = end - prefix
    alternatives = sum(
        text[:start] + correction[start : start + len(replacement)] + text[start + width :]
        == correction
        for start in range(len(text) - width + 1)
    )
    old = text[prefix:end]
    if alternatives != 1 or set(old) & set(replacement):
        return {"status": "ambiguous_requires_human", "edits": []}
    return {
        "status": "draft_unique_splice",
        "edits": [{"range": source.range_dict(prefix, end), "text": replacement}],
    }


def _candidates(
    recipe: dict[str, Any], files: dict[str, bytes], pair_format: str = "plain"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    references: dict[str, Any] = {}
    if recipe["kind"] in {"markdown", "code"}:
        for path, raw in files.items():
            extension = Path(path).suffix
            if path == recipe["license_path"] or extension not in {
                ".md",
                ".rs",
                ".cs",
                ".cpp",
                ".h",
                ".hpp",
            }:
                continue
            document = _document(recipe, path, raw, _url(recipe, path))
            source = source_map(document)
            if recipe["kind"] == "markdown":
                fragments, diagnostics = extract_markdown(source)
            else:
                extraction = extract_code(source, extension, ExtractSettings(header_language="cpp"))
                fragments, diagnostics = extraction.fragments, extraction.diagnostics
            documents.append(document)
            for fragment in fragments:
                if not re.search("[А-Яа-яЁё]", fragment.text):
                    continue
                case = _case(
                    recipe,
                    document,
                    source.range_dict(fragment.start_char, fragment.end_char),
                    "documentation"
                    if recipe["kind"] == "markdown"
                    else "comment"
                    if "comment" in fragment.kind
                    else "message",
                    [source.range_dict(p.start_char, p.end_char) for p in fragment.protected],
                )
                cases.append(case)
            references[document["id"]] = {"extraction_diagnostics": diagnostics}
    else:
        path = "data/GitHubTypoCorpusRu/test.json"
        references["unsupported_rows"] = []
        for number, line in enumerate(files[path].decode("utf-8-sig").splitlines(), 1):
            row = json.loads(
                line, object_pairs_hook=_unique_objects, parse_constant=_invalid_constant
            )
            if (
                set(row) != {"source", "correction", "domain"}
                or not all(isinstance(v, str) for v in row.values())
                or row["domain"] != "GitHubTypoCorpusRu"
                or not row["source"]
            ):
                raise InputError("Изменён JSONL-контракт GitHubTypoCorpusRu.")
            origin = (
                f"{_url(recipe, path)}#row={number}; UTF-8 source field, not original GitHub file"
            )
            raw = row["source"].encode("utf-8")
            encoding, _, _, reason = _classify_encoding(raw)
            if reason or not encoding:
                references["unsupported_rows"].append(
                    {
                        "row": number,
                        "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "reason": reason,
                    }
                )
                continue
            # The original file format is unknown. txt is an explicit source-field
            # transport, not a claim about the original GitHub extension.
            extension = "txt" if pair_format == "plain" else "md"
            document = _document(recipe, f"records/{number:04d}.{extension}", raw, origin)
            document["provenance"]["transformation"] += f"; generated {pair_format} adapter"
            source = source_map(document)
            case = _case(recipe, document, source.range_dict(0, len(source.text)), "other", [])
            documents.append(document)
            cases.append(case)
            references[case["id"]] = {
                "row": number,
                "correction": row["correction"],
                "proposal": pair_proposal(source, row["correction"]),
            }
    return documents, cases, references


def import_source(
    name: str,
    destination: Path,
    limit: int = 250,
    cache: Path | None = None,
    pair_format: str = "plain",
) -> dict[str, Any]:
    if name not in recipes() or not 1 <= limit <= 10000 or pair_format not in {"plain", "markdown"}:
        raise InputError("Неизвестный источник или limit вне 1–10000.")
    recipe = recipes()[name]
    _validate_recipe(recipe)
    files = {}
    for file in recipe["files"]:
        if cache:
            with (cache / safe_path(file["path"])).open("rb") as cache_stream:
                raw = cache_stream.read(MAX_BYTES + 1)
        else:
            raw = _download(_url(recipe, file["path"]))
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != file["sha256"]:
            raise InputError("Байты/права pinned источника изменились; импорт отклонён.")
        files[file["path"]] = raw
    documents, candidates, references = _candidates(recipe, files, pair_format)
    maps = {doc["id"]: source_map(doc) for doc in documents}
    seen = set()
    cases = []

    # Stable hash order, no dependence on predictions or correction presence.
    def sampling_key(case: dict[str, Any]) -> str:
        if recipe["kind"] != "pairs":
            return str(case["id"])
        # Preserve the old pilot's row selection across transport adapters. The
        # legacy identifier is a sampling key only, not an original-format claim.
        row = references[case["id"]]["row"]
        legacy = digest({"source": recipe["id"], "path": f"records/{row:04d}.md"})[:24]
        return digest({"document": legacy, "range": case["range"]})[:24]

    for case in sorted(candidates, key=sampling_key):
        source = maps[case["document"]]
        location = case["range"]
        text = source.raw[location["start"]["byte_offset"] : location["end"]["byte_offset"]].decode(
            source.encoding
        )
        key = " ".join(re.findall(r"\w+", text.casefold()))
        if key not in seen:
            seen.add(key)
            cases.append(case)
    selected = cases[:limit]
    ids = {case["document"] for case in selected}
    corpus = validate_corpus(
        {
            "schema": "language-review.corpus",
            "schema_version": "1.0",
            "id": name,
            "version": recipe["revision"],
            "annotation_version": "draft-1",
            "split_policy": {
                "strategy": "one repository family per split; stable hash sampling; "
                "exact normalized duplicates removed; "
                "existing cross-split near-duplicate validator",
                "test_frozen": False,
                "test_sha256": None,
                "holdout_policy": "upstream test preserved; public contamination possible; "
                "no answers in prompts/retrieval/rule tuning",
            },
            "documents": [doc for doc in documents if doc["id"] in ids],
            "cases": selected,
        }
    )
    manifest = {
        "source": recipe,
        "files": [{**file, "url": _url(recipe, file["path"])} for file in recipe["files"]],
        "corpus_sha256": digest(corpus),
        "candidates": len(candidates),
        "unique_candidates": len(cases),
        "selected": len(selected),
        "unsupported_rows": references.get("unsupported_rows", []),
        "limit": limit,
        "pair_format": pair_format if recipe["kind"] == "pairs" else None,
        "annotation_status": "draft; no independently verified categories or clean attestations",
    }
    destination.mkdir(parents=True, exist_ok=False)
    for path, raw in files.items():
        target = destination / "upstream" / safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    save_new(destination / "corpus.json", corpus)
    save_new(destination / "provenance.json", manifest)
    save_new(destination / "references.json", references)
    return manifest
