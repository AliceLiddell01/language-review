from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from language_review.config import LoadedConfig
from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.extract_code import extract_code
from language_review.extract_docs import extract_markdown, extract_markup_text, extract_plain_text
from language_review.extract_models import ExtractDocument, ExtractResult, FragmentDraft
from language_review.extract_report import resolve_fragments
from language_review.extract_structured import STRUCTURED_GRAMMARS, extract_structured
from language_review.scanner import FileRecord, ScanResult, scan_paths

CODE_EXTENSIONS = {
    ".rs",
    ".cs",
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hh",
    ".hxx",
    ".ps1",
    ".psm1",
    ".psd1",
}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
RST_EXTENSIONS = {".rst"}
ASCIIDOC_EXTENSIONS = {".adoc"}
PLAIN_EXTENSIONS = {".txt"}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path_key = os.path.normcase(os.path.abspath(path))
        root_key = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _record_source(record: FileRecord, root_entry: dict[str, Any]) -> bytes:
    root_path = Path(str(root_entry["path"])).resolve(strict=True)
    if root_entry.get("kind") == "file":
        target = root_path
    elif root_entry.get("kind") == "directory":
        relative = PurePosixPath(record.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise InputError("Путь сканера вышел за пределы корня.")
        target = (root_path / Path(*relative.parts)).resolve(strict=True)
        if not _is_within(target, root_path):
            raise InputError("Символическая ссылка выходит за пределы корня.")
    else:
        raise InputError("Для записи сканирования отсутствует доступный корень.")
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise InputError("Исходный путь больше не является обычным файлом.")
    if record.size_bytes is None or metadata.st_size != record.size_bytes:
        raise InputError("Размер файла изменился после инвентаризации.")
    with target.open("rb") as stream:
        raw = stream.read(record.size_bytes + 1)
    if len(raw) != record.size_bytes:
        raise InputError("Содержимое файла изменилось после инвентаризации.")
    if record._source_sha256 is None or hashlib.sha256(raw).hexdigest() != record._source_sha256:
        raise InputError("Файл изменился после инвентаризации.")
    return raw


def _document_id(root_id: str, path: str, source_hash: str | None) -> str:
    payload = "\0".join((root_id, path, source_hash or "unavailable")).encode("utf-8")
    return "doc-" + hashlib.sha256(payload).hexdigest()[:24]


def _safe_relative_path(record: FileRecord, root_entry: dict[str, Any]) -> str:
    path = PurePosixPath(record.path.replace("\\", "/"))
    if root_entry.get("kind") == "file" and (path.is_absolute() or ".." in path.parts):
        return PurePosixPath(str(root_entry.get("path", ""))).name or "."
    if path.is_absolute() or ".." in path.parts:
        return "."
    return path.as_posix()


def _root_lookup(
    scan: ScanResult,
) -> tuple[
    list[dict[str, Any]],
    dict[str, tuple[str, dict[str, Any]]],
    dict[str, list[tuple[str, dict[str, Any]]]],
]:
    public_roots: list[dict[str, Any]] = []
    lookup: dict[str, tuple[str, dict[str, Any]]] = {}
    duplicates: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for index, root in enumerate(scan.roots, start=1):
        root_id = f"root-{index:04d}"
        public_roots.append(
            {
                "id": root_id,
                "kind": str(root.get("kind", "unknown")),
                "status": str(root.get("status", "unknown")),
                "profile": root.get("profile"),
            }
        )
        if root.get("kind") == "duplicate":
            duplicates.setdefault(str(root.get("path", "")), []).append((root_id, root))
            continue
        lookup.setdefault(str(root.get("path", "")), (root_id, root))
    return public_roots, lookup, duplicates


def _adapt(
    source: SourceMap, extension: str, config: LoadedConfig, profile_name: str | None
) -> tuple[str | None, str | None, list[FragmentDraft], list[str], bool, bool]:
    settings, _ = config.extract_settings_for_profile(profile_name)
    if extension in CODE_EXTENSIONS:
        if extension == ".psd1":
            return (
                "powershell_data",
                "powershell_data",
                [],
                ["powershell_data_file_not_supported"],
                False,
                True,
            )
        result = extract_code(source, extension, settings)
        return (
            result.language,
            "source_code",
            result.fragments,
            result.diagnostics,
            result.partial,
            False,
        )
    if extension in MARKDOWN_EXTENSIONS:
        fragments, diagnostics = extract_markdown(source)
        return "markdown", "markdown", fragments, diagnostics, bool(diagnostics), False
    if extension in PLAIN_EXTENSIONS:
        fragments, diagnostics = extract_plain_text(source)
        return "text", "plain_text", fragments, diagnostics, False, False
    if extension in RST_EXTENSIONS:
        fragments, diagnostics = extract_markup_text(source, "rst")
        return (
            "restructured_text",
            "restructured_text",
            fragments,
            diagnostics,
            bool(diagnostics),
            False,
        )
    if extension in ASCIIDOC_EXTENSIONS:
        fragments, diagnostics = extract_markup_text(source, "adoc")
        return "asciidoc", "asciidoc", fragments, diagnostics, bool(diagnostics), False
    if extension in STRUCTURED_GRAMMARS:
        language, fragments, diagnostics, partial = extract_structured(source, extension, settings)
        unsupported = diagnostics == ["selectors_not_configured"]
        return language, language, fragments, diagnostics, partial, unsupported
    return None, None, [], ["unsupported_file_format"], False, True


def extract_paths(
    paths: list[str],
    config: LoadedConfig,
    *,
    requested_profile: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ExtractResult:
    scan = scan_paths(
        paths,
        config,
        requested_profile=requested_profile,
        cli_overrides=cli_overrides,
    )
    roots, root_lookup, duplicate_roots = _root_lookup(scan)
    documents: list[ExtractDocument] = []
    for record in scan.files:
        if record.reason == "duplicate_argument" and duplicate_roots.get(record.root):
            root_id, root_entry = duplicate_roots[record.root].pop(0)
        else:
            root_id, root_entry = root_lookup.get(
                record.root,
                ("root-0000", {"kind": "invalid", "status": "error", "path": ""}),
            )
        relative_path = _safe_relative_path(record, root_entry)
        document_id = _document_id(root_id, relative_path, record._source_sha256)
        document = ExtractDocument(
            id=document_id,
            root=root_id,
            path=relative_path,
            language=None,
            format=None,
            profile=record.profile,
            status="skipped",
            reason=record.reason,
            source_sha256=record._source_sha256,
            encoding=record.encoding,
            bom=record.bom,
        )
        if record.status == "error":
            document.status = "error"
            document.reason = "scan_error"
            document.diagnostics.append("scan_error")
            if record.reason:
                document.diagnostics.append(record.reason)
            documents.append(document)
            continue
        if record.status != "selected":
            document.status = "skipped"
            documents.append(document)
            continue
        try:
            raw = _record_source(record, root_entry)
            if record.encoding is None:
                raise InputError("Для выбранного файла отсутствует кодировка.")
            source = SourceMap.create(raw, record.encoding, record.bom)
        except OSError, UnicodeError, ValueError, InputError:
            document.status = "error"
            document.reason = "source_unavailable_or_changed"
            document.diagnostics.append(document.reason)
            documents.append(document)
            continue
        extension = PurePosixPath(record.path).suffix.lower()
        try:
            language, file_format, fragments, diagnostics, partial, unsupported = _adapt(
                source, extension, scan.project_configs.get(record.root, config), record.profile
            )
        except Exception:
            document.status = "error"
            document.reason = "extractor_failure"
            document.diagnostics.append(document.reason)
            documents.append(document)
            continue
        try:
            resolved = resolve_fragments(source, fragments)
        except ValueError:
            document.status = "error"
            document.reason = "fragment_source_mismatch"
            document.diagnostics.append(document.reason)
            documents.append(document)
            continue
        document.language = language
        document.format = file_format
        document.diagnostics.extend(diagnostics)
        if unsupported:
            document.status = "unsupported"
            document.reason = diagnostics[0] if diagnostics else "unsupported_file_format"
            document.fragments = resolved
        elif partial:
            document.status = "partial"
            document.reason = "incomplete_parse_or_mapping"
            document.fragments = resolved
        else:
            document.status = "extracted"
            document.reason = None
            document.fragments = resolved
        documents.append(document)
    documents.sort(key=lambda item: (item.root, item.path.casefold(), item.path, item.id))
    return ExtractResult(roots=roots, documents=documents, scan_result=scan)
