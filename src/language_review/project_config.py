"""Репозиторная политика: ограниченный YAML, явные границы и эффективные профили."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from language_review.config import (
    ANALYSIS_STRATEGIES,
    CHECK_KEYS,
    PROFILE_NAME,
    CheckSettings,
    ExtractSettings,
    LoadedConfig,
    Profile,
    ScanSettings,
    _validate_extract,
    _validate_scan,
    validate_check_settings,
    validate_check_value,
)
from language_review.errors import ConfigurationError

FILENAME = ".language-review.yaml"
MAX_CONFIG_BYTES = 262_144
MAX_PROMPT_BYTES = 65_536
ENVIRONMENT_KEYS = {
    "allow_remote_endpoints",
    "llm.chatgpt.allow_cloud",
    "llm.chatgpt.allow_memory",
    "llm.llama_cpp.endpoint",
    "llm.ollama.endpoint",
    "languagetool.endpoint",
    "vale.program",
    "vale.config",
}
TEMPLATE = """# Политика языкового анализа этого репозитория. Секреты хранятся вне проекта.
schema_version: 1
default_profile: everyday
profiles:
  everyday:
    engines: [languagetool, vale, llm]
    analysis:
      mode: normal
    llm:
      provider: llama_cpp
"""


def repository_root(target: str | Path) -> Path:
    """Git/worktree: ближайший .git; вне Git: каталог цели или родитель файла."""
    try:
        path = Path(target).expanduser().resolve(strict=True)
        if not path.is_file() and not path.is_dir():
            raise ConfigurationError("Цель должна быть обычным файлом или каталогом.")
        start = path if path.is_dir() else path.parent
        for parent in (start, *start.parents):
            if (parent / ".git").exists():
                return parent
        return start
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Цель недоступна: {target}.") from exc


def _read(path: Path, limit: int, label: str) -> str:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ConfigurationError(f"{label}: превышен предел {limit} байт.")
        value = raw.decode("utf-8-sig")
        if not value.strip() or "\x00" in value:
            raise ConfigurationError(f"{label}: требуется непустой текст UTF-8 без NUL.")
        return value
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"{label}: не удалось прочитать файл UTF-8.") from exc


def _mapping(value: Any, label: str, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label}: ожидается таблица со строковыми ключами.")
    if keys is not None and (unknown := set(value) - keys):
        raise ConfigurationError(f"{label}: неизвестные ключи {', '.join(sorted(unknown))}.")
    return value


def _parse_yaml(text: str) -> dict[str, Any]:
    parser = YAML(typ="safe", pure=True)
    parser.allow_duplicate_keys = False
    try:
        depth = nodes = 0
        for event in parser.parse(text):
            nodes += 1
            if getattr(event, "anchor", None) or getattr(event, "tag", None):
                raise ConfigurationError(f"{FILENAME}: теги, anchors и aliases запрещены.")
            kind = type(event).__name__
            if kind in {"MappingStartEvent", "SequenceStartEvent"}:
                depth += 1
            elif kind in {"MappingEndEvent", "SequenceEndEvent"}:
                depth -= 1
            if depth > 16 or nodes > 10_000:
                raise ConfigurationError(f"{FILENAME}: превышен предел сложности YAML.")
        return _mapping(
            parser.load(text),
            FILENAME,
            {
                "schema_version",
                "default_profile",
                "profiles",
                "path_rules",
                "prompts",
                "scan",
                "extract",
                "project_id",
            },
        )
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f", строка {mark.line + 1}, столбец {mark.column + 1}" if mark else ""
        raise ConfigurationError(
            f"{FILENAME}{location}: некорректный YAML или повтор ключа."
        ) from exc


def _yaml(path: Path) -> dict[str, Any]:
    return _parse_yaml(_read(path, MAX_CONFIG_BYTES, FILENAME))


def _relative(value: Any, label: str, *, pattern: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or "\n" in value
        or PureWindowsPath(value).drive
        or PurePosixPath(value).is_absolute()
        or any(part in {"..", ".", ""} for part in value.split("/"))
        or (not pattern and any(char in value for char in "*?[]"))
    ):
        raise ConfigurationError(f"{label}: нужен относительный путь от корня с разделителем /.")
    # Ограниченная грамматика glob даёт проверяемую конфликтную семантику.
    if pattern and not (value.endswith("/**") or not any(c in value for c in "*?[]")):
        raise ConfigurationError(f"{label}: допустим точный путь либо префикс каталога с /**.")
    if pattern and any(c in value.removesuffix("/**") for c in "*?[]"):
        raise ConfigurationError(f"{label}: wildcard допустим только в конечном /**.")
    return value


def _inside(root: Path, relative: str) -> Path:
    try:
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ConfigurationError("Файл промпта должен находиться внутри репозитория.")
        return target
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Файл промпта недоступен: {relative}.") from exc


def _prompt_entries(raw: Any, label: str) -> list[tuple[str, Any]]:
    raw = _mapping(raw, label, {"primary", "strategies", "verifier"})
    flat = {key: value for key, value in raw.items() if key != "strategies"}
    if "strategies" in raw:
        flat.update(_mapping(raw["strategies"], label + ".strategies", set(ANALYSIS_STRATEGIES)))
    return list(flat.items())


def _parse_prompt_value(value: Any, label: str) -> tuple[str, str | None]:
    if value is None:
        return "builtin", None
    if isinstance(value, str):
        value = {"file": value}
    value = _mapping(value, label, {"text", "file"})
    if len(value) != 1:
        raise ConfigurationError(f"{label}: задайте ровно text или file.")
    if "file" in value:
        relative = _relative(value["file"], label + ".file")
        if not relative.lower().endswith(".md"):
            raise ConfigurationError(f"{label}: файл промпта должен быть Markdown (.md).")
        return "file", relative
    text = value["text"]
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise ConfigurationError(f"{label}: нужен непустой текст без NUL.")
    if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ConfigurationError(f"{label}: превышен предел {MAX_PROMPT_BYTES} байт.")
    return "text", text


def prompt_files(definition: Any) -> list[str]:
    """Возвращает уникальные относительные пути файловых промптов в порядке YAML."""
    definition = _mapping(definition, FILENAME)
    prompt_sets = [("prompts", definition.get("prompts", {}))]
    profiles = _mapping(definition.get("profiles", {}), "profiles")
    for name, raw_profile in profiles.items():
        profile = _mapping(raw_profile, f"profiles.{name}")
        prompt_sets.append((f"profiles.{name}.prompts", profile.get("prompts", {})))

    paths: list[str] = []
    seen: set[str] = set()
    for label, raw in prompt_sets:
        for key, value in _prompt_entries(raw, label):
            kind, parsed = _parse_prompt_value(value, f"{label}.{key}")
            if kind == "file":
                assert parsed is not None
                if parsed not in seen:
                    seen.add(parsed)
                    paths.append(parsed)
    return paths


def read_project_policy_files(path: Path) -> tuple[bytes, dict[str, bytes]]:
    """Читает ограниченную YAML-политику и все её файловые промпты для копирования."""
    text = _read(path, MAX_CONFIG_BYTES, FILENAME)
    definition = _parse_yaml(text)
    root = path.parent.resolve()
    files = {
        relative: _read(_inside(root, relative), MAX_PROMPT_BYTES, relative).encode("utf-8")
        for relative in prompt_files(definition)
    }
    return text.encode("utf-8"), files


def _prompts(
    raw: Any, root: Path, label: str, protected: set[Path]
) -> tuple[dict[str, str | None], dict[str, str]]:
    values: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for key, value in _prompt_entries(raw, label):
        kind, parsed = _parse_prompt_value(value, f"{label}.{key}")
        if kind == "builtin":
            values[key], sources[key] = None, "встроенный промпт"
            continue
        if kind == "file":
            assert parsed is not None
            source_path = _inside(root, parsed)
            text = _read(source_path, MAX_PROMPT_BYTES, f"{label}.{key}")
            protected.update((root / parsed, source_path))
            source = f"{label}.{key}: {parsed}"
        else:
            assert parsed is not None
            text = parsed
            source = f"{label}.{key}: inline"
        values[key], sources[key] = text, source
    return values, sources


def _profile_policy(raw: dict[str, Any], label: str) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"scan", "extract", "prompts"}:
            continue
        if key in {"engines", "categories", "terms"}:
            flat[key] = value
        elif key in {"analysis", "languagetool", "vale"}:
            flat.update(
                {f"{key}.{name}": item for name, item in _mapping(value, label + "." + key).items()}
            )
        elif key == "llm":
            llm = _mapping(value, label + ".llm")
            provider = llm.get("provider", "llama_cpp")
            if not isinstance(provider, str) or provider not in {"llama_cpp", "ollama", "chatgpt"}:
                raise ConfigurationError(f"{label}.llm.provider: неизвестный провайдер.")
            for name, item in llm.items():
                if name == "model":
                    if not isinstance(item, str) or not item.strip():
                        raise ConfigurationError(f"{label}.llm.model: нужна непустая модель.")
                    flat[f"llm.{provider}.model"] = item
                elif name == "reasoning_effort":
                    if provider != "chatgpt":
                        raise ConfigurationError(
                            f"{label}: reasoning_effort поддерживается только ChatGPT."
                        )
                    flat["llm.chatgpt.effort"] = item
                else:
                    flat[f"llm.{name}"] = item
    if unknown := set(flat) - (CHECK_KEYS - ENVIRONMENT_KEYS):
        raise ConfigurationError(
            f"{label}: неподдерживаемые параметры политики {', '.join(sorted(unknown))}."
        )
    return {
        key.replace(".", "_"): validate_check_value(key, value, label)
        for key, value in flat.items()
    }


def matched_rule(
    config: LoadedConfig, relative: str, *, directory: bool = False
) -> tuple[str, str] | None:
    relative_paths = {relative}
    if directory and relative not in {"", "."}:
        relative_paths.add(relative.rstrip("/") + "/")
    matches = [
        (priority, pattern, profile)
        for pattern, profile, priority in config.path_rules
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in relative_paths)
    ]
    if not matches:
        return None
    _, pattern, profile = max(matches)
    return pattern, profile


def path_profile(
    config: LoadedConfig,
    target: Path,
    forced: str | None = None,
    *,
    directory: bool = False,
) -> str:
    if forced is not None:
        if forced not in config.profiles:
            raise ConfigurationError(f"Профиль «{forced}» не найден в {FILENAME}.")
        return forced
    assert config.project_root is not None and config.default_profile is not None
    relative = target.relative_to(config.project_root).as_posix()
    rule = matched_rule(config, relative, directory=directory)
    return rule[1] if rule else config.default_profile


def load_project(environment: LoadedConfig, target: str | Path) -> LoadedConfig:
    try:
        return _load_project(environment, target)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ConfigurationError(
            f"{FILENAME}: неверный тип значения или недоступный путь."
        ) from exc


def _load_project(environment: LoadedConfig, target: str | Path) -> LoadedConfig:
    root = repository_root(target)
    path = root / FILENAME
    if not path.is_file() or path.name not in {item.name for item in root.iterdir()}:
        raise ConfigurationError(
            f"В корне {root} обязателен {FILENAME}; создайте его командой init."
        )
    if not path.resolve().is_relative_to(root):
        raise ConfigurationError(f"{FILENAME}: конфигурация выходит за пределы репозитория.")
    raw = _yaml(path)
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ConfigurationError(f"{FILENAME}.schema_version: поддерживается только версия 1.")
    profiles_raw = _mapping(raw.get("profiles"), "profiles")
    default = raw.get("default_profile")
    if not isinstance(default, str) or default not in profiles_raw:
        raise ConfigurationError("default_profile должен ссылаться на объявленный профиль.")
    base = CheckSettings(
        **{key.replace(".", "_"): environment.check.value(key) for key in ENVIRONMENT_KEYS}
    )
    protected = {path, path.resolve()}
    common, common_sources = _prompts(raw.get("prompts", {}), root, "prompts", protected)
    profiles: dict[str, Profile] = {}
    sources: dict[str, dict[str, str]] = {}
    for name, definition in profiles_raw.items():
        if not PROFILE_NAME.fullmatch(name):
            raise ConfigurationError("profiles: недопустимое имя профиля.")
        label = f"profiles.{name}"
        definition = _mapping(
            definition,
            label,
            {
                "engines",
                "categories",
                "terms",
                "analysis",
                "llm",
                "languagetool",
                "vale",
                "scan",
                "extract",
                "prompts",
            },
        )
        policy = _profile_policy(definition, label)
        prompts, prompt_sources = _prompts(
            definition.get("prompts", {}), root, label + ".prompts", protected
        )
        effective = common | prompts
        policy.update(
            prompt_primary=effective.get("primary"),
            prompt_verifier=effective.get("verifier"),
            prompt_strategies={
                key: value
                for key, value in effective.items()
                if key in ANALYSIS_STRATEGIES and value is not None
            },
            prompt_sources=common_sources | prompt_sources,
        )
        validate_check_settings(replace(base, **policy))
        profiles[name] = Profile(
            name,
            0,
            (),
            (),
            _validate_scan(definition.get("scan", {}), label + ".scan", partial=True),
            _validate_extract(definition.get("extract", {}), label + ".extract", partial=True),
            policy,
        )
        sources[name] = {
            f"check.{key}": f"{FILENAME}:{label}"
            for key in CHECK_KEYS
            if key.replace(".", "_") in policy
        }
        sources[name].update(
            {key: f"{FILENAME}:{label}.scan" for key in profiles[name].scan_overrides}
        )
        sources[name].update(
            {
                f"extract.{key}": f"{FILENAME}:{label}.extract"
                for key in profiles[name].extract_overrides
            }
        )
    rules_raw = raw.get("path_rules", [])
    if not isinstance(rules_raw, list) or len(rules_raw) > 256:
        raise ConfigurationError("path_rules: нужен список не более 256 правил.")
    rules: list[tuple[str, str, int]] = []
    for index, rule in enumerate(rules_raw):
        label = f"path_rules[{index}]"
        rule = _mapping(rule, label, {"path", "profile", "priority"})
        pattern = _relative(rule.get("path"), label + ".path", pattern=True)
        profile, priority = rule.get("profile"), rule.get("priority", 0)
        if not isinstance(profile, str) or profile not in profiles or type(priority) is not int:
            raise ConfigurationError(
                f"{label}: неверная ссылка profile или целочисленный priority."
            )
        prefix = pattern.removesuffix("**")
        for other, _, rank in rules:
            other_prefix = other.removesuffix("**")
            overlaps = (
                pattern == other
                or (pattern.endswith("/**") and other_prefix.startswith(prefix))
                or (other.endswith("/**") and prefix.startswith(other_prefix))
            )
            if pattern == other or (priority == rank and overlaps):
                raise ConfigurationError(
                    f"{label}: конфликт правил {pattern} и {other}; задайте разные priority."
                )
        rules.append((pattern, profile, priority))
    scan_values = _validate_scan(raw.get("scan", {}), "scan", partial=True)
    extract_values = _validate_extract(raw.get("extract", {}), "extract", partial=True)
    identifier = raw.get("project_id")
    if identifier is not None:
        from language_review.memory.settings import project_uuid

        if not isinstance(identifier, str):
            raise ConfigurationError("project_id должен быть UUID.")
        identifier = project_uuid(identifier)
    # UUID из YAML задаёт пространство имён, но разные клоны имеют разные области.
    identity = str(
        uuid5(NAMESPACE_URL, f"language-review:{os.path.normcase(str(root))}:{identifier or ''}")
    )
    return replace(
        environment,
        project_root=root,
        project_id=identity,
        project_files=tuple(sorted(protected)),
        path_rules=tuple(sorted(rules)),
        scan=replace(ScanSettings(), **scan_values),
        extract=replace(ExtractSettings(), **extract_values),
        sources={
            key: FILENAME if key in scan_values else "по умолчанию"
            for key in asdict(ScanSettings())
        },
        extract_sources={key: FILENAME for key in extract_values},
        check=base,
        check_sources={
            key: environment.check_sources.get(key, "по умолчанию")
            if key in ENVIRONMENT_KEYS
            else "по умолчанию"
            for key in CHECK_KEYS
        },
        profiles=profiles,
        profile_sources=sources,
        default_profile=default,
        default_profile_source=FILENAME,
    )


def init_project(target: str | Path) -> Path:
    root = repository_root(target)
    path = root / FILENAME
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(TEMPLATE)
    except OSError as exc:
        raise ConfigurationError(
            f"init: {FILENAME} уже существует или недоступен; перезапись запрещена."
        ) from exc
    return path


def project_mapping(
    config: LoadedConfig, target: str | Path, forced: str | None = None
) -> dict[str, Any]:
    from language_review.check import public_source
    from language_review.config import _check_display_value
    from language_review.engine_llm import effective_prompts, public_model_name

    assert config.project_root is not None
    path = Path(target).resolve(strict=True)
    profile = path_profile(config, path, forced)
    settings, sources = config.check_settings_for_profile(profile)
    relative = path.relative_to(config.project_root).as_posix()
    rule = matched_rule(config, relative) if forced is None else None
    return {
        "schema_version": 1,
        "project": config.project_root.name,
        "configuration": FILENAME,
        "selected_profile": profile,
        "default_profile": config.default_profile,
        "path_rule": rule[0] if rule else None,
        "path_rules": [
            {"path": pattern, "profile": name, "priority": rank}
            for pattern, name, rank in config.path_rules
        ],
        "check": {
            key: {
                "value": len(settings.terms)
                if key == "terms"
                else public_model_name(settings.value(key))
                if key.endswith(".model") or key in {"vale.config", "vale.program"}
                else _check_display_value(key, settings.value(key)),
                "source": public_source(sources.get(key, "по умолчанию")),
            }
            for key in sorted(CHECK_KEYS)
        },
        "prompts": effective_prompts(settings),
        "profiles": sorted(config.profiles),
    }
