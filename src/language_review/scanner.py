from __future__ import annotations

import codecs
import fnmatch
import hashlib
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pathspec import GitIgnoreSpec

from language_review.config import LoadedConfig, Profile, ScanSettings
from language_review.errors import ConfigurationError
from language_review.project_config import load_project, path_profile

FILE_TYPES = {
    ".rs": "rust_source",
    ".cs": "csharp_source",
    ".cpp": "cpp_source",
    ".cc": "cpp_source",
    ".cxx": "cpp_source",
    ".c": "c_source",
    ".h": "c_header",
    ".hpp": "cpp_header",
    ".hh": "cpp_header",
    ".hxx": "cpp_header",
    ".ps1": "powershell_source",
    ".psm1": "powershell_source",
    ".psd1": "powershell_data",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "plain_text",
    ".rst": "restructured_text",
    ".adoc": "asciidoc",
}
UTF32_LE_BOM = codecs.BOM_UTF32_LE
UTF32_BE_BOM = codecs.BOM_UTF32_BE
UTF16_LE_BOM = codecs.BOM_UTF16_LE
UTF16_BE_BOM = codecs.BOM_UTF16_BE
UTF8_BOM = codecs.BOM_UTF8


@dataclass(frozen=True)
class FileRecord:
    root: str
    path: str
    status: str
    reason: str | None = None
    file_type: str | None = None
    size_bytes: int | None = None
    readable: bool | None = None
    encoding: str | None = None
    bom: str | None = None
    line_endings: str | None = None
    profile: str | None = None
    error: str | None = None
    _source_sha256: str | None = field(default=None, repr=False)


@dataclass
class ScanResult:
    roots: list[dict[str, Any]]
    files: list[FileRecord]
    protected_paths: set[str]
    input_errors: bool = False
    project_configs: dict[str, LoadedConfig] = field(default_factory=dict, repr=False)

    @property
    def error_count(self) -> int:
        return sum(record.status == "error" for record in self.files)

    @property
    def skipped_count(self) -> int:
        return sum(record.status == "skipped" for record in self.files)

    @property
    def selected_count(self) -> int:
        return sum(record.status == "selected" for record in self.files)

    @property
    def status(self) -> str:
        if self.input_errors:
            return "input_error"
        if self.error_count:
            return "partial"
        return "ok"


@dataclass(frozen=True)
class IgnoreContext:
    base: Path
    spec: GitIgnoreSpec


def apply_cli_overrides(settings: ScanSettings, overrides: dict[str, Any] | None) -> ScanSettings:
    if not overrides:
        return settings
    values: dict[str, Any] = {}
    only_extensions = overrides.get("only_extensions")
    additions = tuple(overrides.get("extensions_add", ()))
    if only_extensions is not None:
        values["extensions"] = tuple(dict.fromkeys(only_extensions))
    elif additions:
        values["extensions"] = tuple(dict.fromkeys((*settings.extensions, *additions)))
    excludes = tuple(overrides.get("exclude_add", ()))
    if excludes:
        values["exclude_patterns"] = tuple(dict.fromkeys((*settings.exclude_patterns, *excludes)))
    for key in ("respect_gitignore", "follow_symlinks", "max_file_size"):
        value = overrides.get(key)
        if value is not None:
            values[key] = value
    return replace(settings, **values)


def _normalize_root(raw: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(raw).expanduser())))


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return False


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path_key = os.path.normcase(os.path.abspath(path))
        root_key = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _posix_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _clean_pattern(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _glob_matches(relative: str, patterns: tuple[str, ...]) -> bool:
    normalized = _clean_pattern(relative)
    parts = normalized.split("/")
    for raw_pattern in patterns:
        pattern = _clean_pattern(raw_pattern)
        if "/" in pattern:
            if fnmatch.fnmatchcase(normalized, pattern):
                return True
        elif any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
    return False


def _profile_matches_marker(directory: Path, pattern: str) -> bool:
    try:
        return any(directory.glob(pattern))
    except OSError, ValueError:
        return False


def _project_root(start: Path) -> Path:
    current = start
    filesystem_root = Path(start.anchor) if start.anchor else start
    while True:
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return filesystem_root
        current = parent


def _gitignore_root(start: Path) -> Path:
    current = start
    while True:
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return start
        current = parent


def _choose_profile(config: LoadedConfig, root: Path, requested_profile: str | None) -> str | None:
    if requested_profile is not None:
        if requested_profile not in config.profiles:
            raise ConfigurationError(f"Профиль «{requested_profile}» не найден.")
        return requested_profile
    if config.default_profile is not None:
        return config.default_profile
    directory = root if root.is_dir() else root.parent
    project_root = _project_root(directory)
    current = directory
    while True:
        matches = [
            profile
            for profile in config.profiles.values()
            if profile.project_markers
            and any(_profile_matches_marker(current, marker) for marker in profile.project_markers)
        ]
        if matches:
            highest = max(profile.priority for profile in matches)
            winners = sorted(profile.name for profile in matches if profile.priority == highest)
            if len(winners) > 1:
                raise ConfigurationError(
                    f"Профили {', '.join(winners)} одинакового приоритета совпали с "
                    f"проектом {current}; задайте разный priority."
                )
            return winners[0]
        if current == project_root or current.parent == current:
            break
        current = current.parent
    return None


def _profile_for_path(
    config: LoadedConfig,
    relative: str,
    root_profile: str | None,
    explicit_profile: bool,
) -> str | None:
    if explicit_profile:
        return root_profile
    candidates: dict[str, Profile] = {}
    if root_profile is not None:
        candidates[root_profile] = config.profiles[root_profile]
    for profile in config.profiles.values():
        if profile.match_paths and _glob_matches(relative, profile.match_paths):
            candidates[profile.name] = profile
    if not candidates:
        return None
    highest = max(profile.priority for profile in candidates.values())
    winners = sorted(name for name, profile in candidates.items() if profile.priority == highest)
    if len(winners) > 1:
        raise ConfigurationError(
            f"Профили {', '.join(winners)} одинакового приоритета совпали с путём {relative}; "
            "задайте разный priority."
        )
    return winners[0]


def _classify_encoding(data: bytes) -> tuple[str | None, str | None, str | None, str | None]:
    if data.startswith(UTF32_LE_BOM):
        encoding, bom, payload = "utf-32-le", "utf-32-le", data[len(UTF32_LE_BOM) :]
    elif data.startswith(UTF32_BE_BOM):
        encoding, bom, payload = "utf-32-be", "utf-32-be", data[len(UTF32_BE_BOM) :]
    elif data.startswith(UTF8_BOM):
        encoding, bom, payload = "utf-8", "utf-8", data[len(UTF8_BOM) :]
    elif data.startswith(UTF16_LE_BOM):
        encoding, bom, payload = "utf-16-le", "utf-16-le", data[len(UTF16_LE_BOM) :]
    elif data.startswith(UTF16_BE_BOM):
        encoding, bom, payload = "utf-16-be", "utf-16-be", data[len(UTF16_BE_BOM) :]
    else:
        encoding, bom, payload = "utf-8", None, data

    if bom is None and b"\x00" in data:
        return None, None, None, "binary_content"
    try:
        text = payload.decode(encoding, errors="strict")
    except UnicodeError:
        if bom is not None:
            return encoding, bom, None, "invalid_encoding"
        try:
            text = data.decode("cp1251", errors="strict")
        except UnicodeError:
            return None, None, None, "invalid_encoding"
        if any(not (character.isprintable() or character in "\r\n\t\f") for character in text):
            return None, None, None, "binary_content"
        if not any(ord(character) >= 128 for character in text):
            return None, None, None, "invalid_encoding"
        encoding = "windows-1251"

    if any(not (character.isprintable() or character in "\r\n\t\f") for character in text):
        return None, None, None, "binary_content"
    return encoding, bom, text, None


def _line_endings(text: str) -> str:
    if not text:
        return "none"
    crlf = text.count("\r\n")
    remainder = text.replace("\r\n", "")
    lf = remainder.count("\n")
    cr = remainder.count("\r")
    kinds = sum(count > 0 for count in (crlf, lf, cr))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    if cr:
        return "cr"
    return "none"


def _file_type(path: Path) -> str:
    return FILE_TYPES.get(path.suffix.lower(), "text_file")


def _append_error(
    result: ScanResult,
    root: str,
    path: str,
    message: str,
    *,
    profile: str | None = None,
) -> None:
    result.files.append(
        FileRecord(
            root=root,
            path=path,
            status="error",
            reason="io_error",
            profile=profile,
            error=message,
        )
    )


def _load_ignore_context(
    directory: Path,
    root_path: str,
    root_scope: Path,
    result: ScanResult,
) -> IgnoreContext | None:
    ignore_path = directory / ".gitignore"
    try:
        raw = ignore_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        relative = _posix_relative(ignore_path, root_scope)
        _append_error(result, root_path, relative, f"Не удалось прочитать .gitignore: {exc}")
        return None
    try:
        text = raw.decode("utf-8-sig")
        spec = GitIgnoreSpec.from_lines(text.splitlines(), backend="simple")
    except (UnicodeError, ValueError) as exc:
        relative = _posix_relative(ignore_path, root_scope)
        _append_error(result, root_path, relative, f"Некорректный .gitignore: {exc}")
        return None
    result.protected_paths.add(_path_key(ignore_path))
    if len(spec) == 0:
        return None
    return IgnoreContext(directory, spec)


def _ignore_contexts_before(
    base: Path, directory: Path, root_path: str, root_scope: Path, result: ScanResult
) -> list[IgnoreContext]:
    try:
        relative_parts = directory.relative_to(base).parts
    except ValueError:
        return []
    contexts: list[IgnoreContext] = []
    current = base
    context = _load_ignore_context(current, root_path, root_scope, result)
    if context is not None:
        contexts.append(context)
    for part in relative_parts:
        current = current / part
        context = _load_ignore_context(current, root_path, root_scope, result)
        if context is not None:
            contexts.append(context)
    return contexts


def _spec_ignores(context: IgnoreContext, path: Path, is_directory: bool) -> bool | None:
    try:
        relative = path.relative_to(context.base).as_posix()
    except ValueError:
        return None
    parts = relative.split("/")
    ignored: bool | None = None
    for index in range(len(parts)):
        prefix = "/".join(parts[: index + 1])
        prefix_is_directory = index < len(parts) - 1 or is_directory
        candidate = f"{prefix}/" if prefix_is_directory else prefix
        check = context.spec.check_file(candidate)
        if check.include is None:
            continue
        current_ignored = check.include
        if current_ignored and prefix_is_directory:
            return True
        ignored = current_ignored
    return ignored


def _is_gitignored(contexts: list[IgnoreContext], path: Path, is_directory: bool) -> bool:
    ignored = False
    for context in contexts:
        match = _spec_ignores(context, path, is_directory)
        if match is not None:
            ignored = match
    return ignored


def _is_excluded(relative: str, settings: ScanSettings, is_directory: bool) -> bool:
    if is_directory:
        name = relative.rstrip("/").rsplit("/", 1)[-1].casefold()
        if name in {item.casefold() for item in settings.exclude_dirs}:
            return True
    return _glob_matches(relative, settings.exclude_patterns)


def _decode_record(
    path: Path,
    data: bytes,
    root_path: str,
    relative: str,
    profile_name: str | None,
) -> FileRecord:
    encoding, bom, text, reason = _classify_encoding(data)
    if reason is not None:
        return FileRecord(
            root=root_path,
            path=relative,
            status="skipped",
            reason=reason,
            file_type=_file_type(path),
            size_bytes=len(data),
            readable=False,
            encoding=encoding,
            bom=bom,
            profile=profile_name,
            _source_sha256=hashlib.sha256(data).hexdigest(),
        )
    if text is None:
        return FileRecord(
            root=root_path,
            path=relative,
            status="skipped",
            reason="invalid_encoding",
            file_type=_file_type(path),
            size_bytes=len(data),
            readable=False,
            profile=profile_name,
            _source_sha256=hashlib.sha256(data).hexdigest(),
        )
    return FileRecord(
        root=root_path,
        path=relative,
        status="selected",
        file_type=_file_type(path),
        size_bytes=len(data),
        readable=True,
        encoding=encoding,
        bom=bom,
        line_endings=_line_endings(text),
        profile=profile_name,
        _source_sha256=hashlib.sha256(data).hexdigest(),
    )


def _scan_file(
    path: Path,
    root_path: str,
    relative: str,
    settings: ScanSettings,
    profile_name: str | None,
    result: ScanResult,
    seen_files: set[str],
) -> None:
    real_path = path.resolve(strict=False)
    key = _path_key(real_path)
    result.protected_paths.add(key)
    if key in seen_files:
        result.files.append(
            FileRecord(
                root=root_path,
                path=relative,
                status="skipped",
                reason="duplicate_path",
                file_type=_file_type(path),
                profile=profile_name,
            )
        )
        result.protected_paths.add(_path_key(path))
        return
    seen_files.add(key)
    result.protected_paths.add(_path_key(path))
    file_type = _file_type(path)
    if path.suffix.lower() not in settings.extensions:
        result.files.append(
            FileRecord(
                root=root_path,
                path=relative,
                status="skipped",
                reason="unsupported_extension",
                file_type=file_type,
                profile=profile_name,
            )
        )
        return
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            result.files.append(
                FileRecord(
                    root=root_path,
                    path=relative,
                    status="skipped",
                    reason="not_regular_file",
                    file_type=file_type,
                    profile=profile_name,
                )
            )
            return
        if metadata.st_size > settings.max_file_size:
            result.files.append(
                FileRecord(
                    root=root_path,
                    path=relative,
                    status="skipped",
                    reason="file_too_large",
                    file_type=file_type,
                    size_bytes=metadata.st_size,
                    profile=profile_name,
                )
            )
            return
        with path.open("rb") as stream:
            data = stream.read(settings.max_file_size + 1)
    except OSError as exc:
        _append_error(
            result, root_path, relative, f"Не удалось прочитать файл: {exc}", profile=profile_name
        )
        return
    if len(data) > settings.max_file_size:
        result.files.append(
            FileRecord(
                root=root_path,
                path=relative,
                status="skipped",
                reason="file_too_large",
                file_type=file_type,
                size_bytes=len(data),
                profile=profile_name,
            )
        )
        return
    result.files.append(_decode_record(path, data, root_path, relative, profile_name))


def _scan_directory(
    scan_root: Path,
    root_path: str,
    config: LoadedConfig,
    root_profile: str | None,
    requested_profile: str | None,
    cli_overrides: dict[str, Any] | None,
    inherited_contexts: list[IgnoreContext],
    result: ScanResult,
    seen_files: set[str],
    seen_directories: set[str],
) -> None:
    root_key = _path_key(scan_root)
    policy_files = {_path_key(path) for path in config.project_files}
    seen_directories.add(root_key)
    may_respect_gitignore = any(
        apply_cli_overrides(config.settings_for_profile(name)[0], cli_overrides).respect_gitignore
        for name in (None, *config.profiles)
    )
    stack: list[tuple[Path, tuple[str, ...], list[IgnoreContext]]] = [
        (scan_root, (), inherited_contexts)
    ]

    while stack:
        directory, parts, parents = stack.pop()
        relative_directory = "/".join(parts)
        directory_profile = (
            _profile_for_path(
                config,
                relative_directory + ("/" if relative_directory else ""),
                root_profile,
                requested_profile is not None,
            )
            if parts
            else root_profile
        )
        directory_settings, _ = config.settings_for_profile(directory_profile)
        directory_settings = apply_cli_overrides(directory_settings, cli_overrides)
        contexts = list(parents)
        if may_respect_gitignore:
            local_context = _load_ignore_context(directory, root_path, scan_root, result)
            if local_context is not None:
                contexts.append(local_context)

        try:
            with os.scandir(directory) as iterator:
                entries = sorted(list(iterator), key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            relative = relative_directory or "."
            _append_error(
                result,
                root_path,
                relative,
                f"Не удалось обойти каталог: {exc}",
                profile=directory_profile,
            )
            continue

        child_directories: list[tuple[Path, tuple[str, ...], list[IgnoreContext]]] = []
        for entry in entries:
            path = Path(entry.path)
            if _path_key(path) in policy_files:
                continue
            entry_parts = (*parts, entry.name)
            relative = "/".join(entry_parts)
            try:
                is_link = _is_link(path)
                resolved = path.resolve(strict=True) if is_link else path
                is_directory = resolved.is_dir()
                is_file = resolved.is_file()
            except OSError as exc:
                _append_error(
                    result,
                    root_path,
                    relative,
                    f"Не удалось определить тип пути: {exc}",
                    profile=directory_profile,
                )
                continue

            relative_for_profile = relative + ("/" if is_directory else "")
            profile_name = _profile_for_path(
                config,
                relative_for_profile,
                root_profile,
                requested_profile is not None,
            )
            if config.project_root is not None:
                profile_name = path_profile(config, path, requested_profile)
            settings, _ = config.settings_for_profile(profile_name)
            settings = apply_cli_overrides(settings, cli_overrides)

            if is_link and not settings.follow_symlinks:
                result.files.append(
                    FileRecord(
                        root=root_path,
                        path=relative,
                        status="skipped",
                        reason="symbolic_link_not_followed",
                        file_type="directory" if is_directory else _file_type(path),
                        profile=profile_name,
                    )
                )
                result.protected_paths.add(_path_key(path))
                continue
            if is_link and not _is_within(resolved, scan_root):
                result.files.append(
                    FileRecord(
                        root=root_path,
                        path=relative,
                        status="skipped",
                        reason="symlink_outside_root",
                        file_type="directory" if is_directory else _file_type(path),
                        profile=profile_name,
                    )
                )
                result.protected_paths.add(_path_key(path))
                continue

            if is_directory:
                if (path / ".git").exists():
                    result.files.append(
                        FileRecord(
                            root=root_path,
                            path=relative,
                            status="skipped",
                            reason="nested_repository",
                            profile=profile_name,
                        )
                    )
                    continue
                if _is_excluded(relative, settings, is_directory=True):
                    result.files.append(
                        FileRecord(
                            root=root_path,
                            path=relative,
                            status="skipped",
                            reason="excluded_directory",
                            file_type="directory",
                            profile=profile_name,
                        )
                    )
                    continue
                if settings.respect_gitignore and _is_gitignored(contexts, path, True):
                    result.files.append(
                        FileRecord(
                            root=root_path,
                            path=relative,
                            status="skipped",
                            reason="gitignore",
                            file_type="directory",
                            profile=profile_name,
                        )
                    )
                    continue
                real_key = _path_key(resolved)
                if real_key in seen_directories:
                    result.files.append(
                        FileRecord(
                            root=root_path,
                            path=relative,
                            status="skipped",
                            reason="directory_cycle_or_duplicate",
                            file_type="directory",
                            profile=profile_name,
                        )
                    )
                    continue
                seen_directories.add(real_key)
                child_directories.append((resolved, entry_parts, contexts))
            elif is_file:
                result.protected_paths.add(_path_key(path))
                if _is_excluded(relative, settings, is_directory=False):
                    result.files.append(
                        FileRecord(
                            root=root_path,
                            path=relative,
                            status="skipped",
                            reason="excluded_pattern",
                            file_type=_file_type(path),
                            profile=profile_name,
                        )
                    )
                    continue
                if settings.respect_gitignore and _is_gitignored(contexts, path, False):
                    result.files.append(
                        FileRecord(
                            root=root_path,
                            path=relative,
                            status="skipped",
                            reason="gitignore",
                            file_type=_file_type(path),
                            profile=profile_name,
                        )
                    )
                    continue
                _scan_file(path, root_path, relative, settings, profile_name, result, seen_files)
            else:
                result.files.append(
                    FileRecord(
                        root=root_path,
                        path=relative,
                        status="skipped",
                        reason="unsupported_file_kind",
                        profile=profile_name,
                    )
                )

        stack.extend(reversed(child_directories))


def scan_paths(
    paths: list[str],
    config: LoadedConfig,
    *,
    requested_profile: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ScanResult:
    result = ScanResult(roots=[], files=[], protected_paths=set())
    environment = config
    # Все политики валидируются до чтения исходников и до любого вызова движка.
    policies = {raw: load_project(environment, raw) for raw in paths if Path(raw).exists()}
    seen_arguments: set[str] = set()
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    seen_resolved_roots: set[str] = set()

    for raw in paths:
        config = policies.get(raw, environment)
        try:
            input_path = _normalize_root(raw)
        except (OSError, TypeError, ValueError) as exc:
            result.input_errors = True
            result.roots.append({"path": str(raw), "kind": "invalid", "status": "error"})
            _append_error(result, str(raw), str(raw), f"Некорректный путь: {exc}")
            continue

        display_path = str(input_path)
        result.project_configs[display_path] = config
        if config.project_root is not None:
            result.protected_paths.update(_path_key(path) for path in config.project_files)
        argument_key = _path_key(input_path)
        if argument_key in seen_arguments:
            result.roots.append({"path": display_path, "kind": "duplicate", "status": "skipped"})
            result.files.append(
                FileRecord(
                    root=display_path,
                    path=".",
                    status="skipped",
                    reason="duplicate_argument",
                )
            )
            continue
        seen_arguments.add(argument_key)

        try:
            is_link = _is_link(input_path)
            resolved = input_path.resolve(strict=True)
        except OSError as exc:
            result.input_errors = True
            result.roots.append({"path": display_path, "kind": "missing", "status": "error"})
            _append_error(result, display_path, ".", f"Путь не найден или недоступен: {exc}")
            continue

        if not resolved.is_dir() and not resolved.is_file():
            result.roots.append({"path": display_path, "kind": "special", "status": "skipped"})
            result.files.append(
                FileRecord(
                    root=display_path,
                    path=".",
                    status="skipped",
                    reason="unsupported_file_kind",
                )
            )
            continue

        is_directory = resolved.is_dir()
        scope_root = resolved if is_directory else resolved.parent
        root_profile = _choose_profile(config, scope_root, requested_profile)
        base_settings, _ = config.settings_for_profile(root_profile)
        root_settings = apply_cli_overrides(base_settings, cli_overrides)
        if is_link and not root_settings.follow_symlinks:
            result.roots.append({"path": display_path, "kind": "symlink", "status": "skipped"})
            result.files.append(
                FileRecord(
                    root=display_path,
                    path=".",
                    status="skipped",
                    reason="symbolic_link_not_followed",
                    profile=root_profile,
                )
            )
            result.protected_paths.add(_path_key(input_path))
            continue

        real_root_key = _path_key(resolved)
        if real_root_key in seen_resolved_roots:
            result.roots.append({"path": display_path, "kind": "duplicate", "status": "skipped"})
            result.files.append(
                FileRecord(
                    root=display_path,
                    path=".",
                    status="skipped",
                    reason="duplicate_argument",
                )
            )
            continue
        seen_resolved_roots.add(real_root_key)

        root_entry = {
            "path": display_path,
            "kind": "directory" if is_directory else "file",
            "status": "scanning",
            "profile": root_profile,
        }
        result.roots.append(root_entry)
        start_record = len(result.files)

        may_respect_gitignore = any(
            apply_cli_overrides(
                config.settings_for_profile(name)[0], cli_overrides
            ).respect_gitignore
            for name in (None, *config.profiles)
        )
        git_base = _gitignore_root(scope_root)
        context_limit = scope_root.parent if is_directory else scope_root
        contexts = (
            _ignore_contexts_before(
                git_base,
                context_limit,
                display_path,
                scope_root,
                result,
            )
            if may_respect_gitignore
            else []
        )

        if is_directory:
            if _is_excluded(resolved.name, root_settings, is_directory=True):
                result.files.append(
                    FileRecord(
                        root=display_path,
                        path=".",
                        status="skipped",
                        reason="excluded_directory",
                        file_type="directory",
                        profile=root_profile,
                    )
                )
            elif root_settings.respect_gitignore and _is_gitignored(contexts, resolved, True):
                result.files.append(
                    FileRecord(
                        root=display_path,
                        path=".",
                        status="skipped",
                        reason="gitignore",
                        file_type="directory",
                        profile=root_profile,
                    )
                )
            else:
                _scan_directory(
                    resolved,
                    display_path,
                    config,
                    root_profile,
                    requested_profile,
                    cli_overrides,
                    contexts,
                    result,
                    seen_files,
                    seen_directories,
                )
        else:
            relative = resolved.name
            profile_name = _profile_for_path(
                config,
                relative,
                root_profile,
                requested_profile is not None,
            )
            if config.project_root is not None:
                profile_name = path_profile(config, resolved, requested_profile)
            settings, _ = config.settings_for_profile(profile_name)
            settings = apply_cli_overrides(settings, cli_overrides)
            result.protected_paths.add(_path_key(resolved))
            if _path_key(resolved) in {_path_key(path) for path in config.project_files}:
                result.files.append(
                    FileRecord(
                        root=display_path,
                        path=relative,
                        status="skipped",
                        reason="project_configuration",
                        profile=profile_name,
                    )
                )
            elif _is_excluded(relative, settings, is_directory=False):
                result.files.append(
                    FileRecord(
                        root=display_path,
                        path=relative,
                        status="skipped",
                        reason="excluded_pattern",
                        file_type=_file_type(resolved),
                        profile=profile_name,
                    )
                )
            elif settings.respect_gitignore and _is_gitignored(contexts, resolved, False):
                result.files.append(
                    FileRecord(
                        root=display_path,
                        path=relative,
                        status="skipped",
                        reason="gitignore",
                        file_type=_file_type(resolved),
                        profile=profile_name,
                    )
                )
            else:
                _scan_file(
                    resolved,
                    display_path,
                    relative,
                    settings,
                    profile_name,
                    result,
                    seen_files,
                )

        root_records = result.files[start_record:]
        if any(record.status == "error" for record in root_records):
            root_entry["status"] = "partial"
        elif any(
            record.path == "."
            and record.status == "skipped"
            and record.reason in {"excluded_directory", "gitignore"}
            for record in root_records
        ):
            root_entry["status"] = "skipped"
        else:
            root_entry["status"] = "ok"

    result.files.sort(
        key=lambda item: (item.root.casefold(), item.root, item.path.casefold(), item.path)
    )
    return result
