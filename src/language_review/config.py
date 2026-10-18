from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from language_review.errors import ConfigurationError

DEFAULT_EXTENSIONS = (
    ".rs",
    ".cs",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".hh",
    ".hxx",
    ".ps1",
    ".psm1",
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".adoc",
)
DEFAULT_EXCLUDED_DIRS = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "bin",
    "obj",
    "build",
    "dist",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
)
SCAN_KEYS = {
    "extensions",
    "exclude_dirs",
    "exclude_patterns",
    "respect_gitignore",
    "follow_symlinks",
    "max_file_size",
}
EXTRACT_KEYS = {"header_language", "message_calls", "selectors"}
SELECTOR_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".xml"}
ROOT_KEYS = {"scan", "extract", "default_profile", "profiles", "doctor"}
PROFILE_KEYS = {"priority", "project_markers", "match_paths", "scan", "extract"}
DOCTOR_KEYS = {"programs", "http_services"}
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class ScanSettings:
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDED_DIRS
    exclude_patterns: tuple[str, ...] = ()
    respect_gitignore: bool = True
    follow_symlinks: bool = False
    max_file_size: int = 5_000_000


@dataclass(frozen=True)
class ExtractSettings:
    header_language: str = "auto"
    message_calls: tuple[str, ...] = ()
    selectors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def selector_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.selectors)


@dataclass(frozen=True)
class Profile:
    name: str
    priority: int
    project_markers: tuple[str, ...]
    match_paths: tuple[str, ...]
    scan_overrides: dict[str, Any]
    extract_overrides: dict[str, Any]


def _combine_sources(*sources: str | None) -> str:
    parts = [
        part
        for source in sources
        if source is not None and source != "по умолчанию"
        for part in source.split("; ")
        if part
    ]
    return "; ".join(dict.fromkeys(parts)) or "по умолчанию"


@dataclass(frozen=True)
class LoadedConfig:
    scan: ScanSettings
    extract: ExtractSettings
    sources: dict[str, str]
    extract_sources: dict[str, str]
    profiles: dict[str, Profile]
    profile_sources: dict[str, dict[str, str]]
    default_profile: str | None
    default_profile_source: str
    doctor_programs: tuple[str, ...]
    doctor_http_services: dict[str, str]
    doctor_sources: dict[str, str]
    user_config_path: Path
    explicit_config_path: Path | None
    loaded_config_paths: tuple[Path, ...]

    def settings_for_profile(self, profile_name: str | None) -> tuple[ScanSettings, dict[str, str]]:
        settings = self.scan
        sources = dict(self.sources)
        if profile_name is None:
            return settings, sources
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise ConfigurationError(f"Профиль «{profile_name}» не найден.")
        settings = replace(settings, **profile.scan_overrides)
        for key in profile.scan_overrides:
            sources[key] = self.profile_sources.get(profile_name, {}).get(
                key, f"профиль: {profile_name}"
            )
        return settings, sources

    def extract_settings_for_profile(
        self, profile_name: str | None
    ) -> tuple[ExtractSettings, dict[str, str]]:
        settings = self.extract
        sources = dict(self.extract_sources)
        if profile_name is None:
            return settings, sources
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise ConfigurationError(f"Профиль «{profile_name}» не найден.")
        overrides = dict(profile.extract_overrides)
        base_message_calls = settings.message_calls
        profile_message_calls = tuple(overrides.get("message_calls", ()))
        if "selectors" in overrides:
            selectors = settings.selector_map()
            selectors.update(overrides.pop("selectors"))
            overrides["selectors"] = selectors
        if "message_calls" in overrides:
            overrides["message_calls"] = tuple(
                dict.fromkeys((*settings.message_calls, *overrides["message_calls"]))
            )
        settings = replace(settings, **overrides)
        profile_sources = self.profile_sources.get(profile_name, {})
        for key in profile.extract_overrides:
            if key == "selectors":
                for extension in profile.extract_overrides["selectors"]:
                    sources[f"selectors.{extension}"] = profile_sources.get(
                        f"extract.selectors.{extension}", f"профиль: {profile_name}"
                    )
            elif key == "message_calls":
                profile_source = profile_sources.get(
                    "extract.message_calls", f"профиль: {profile_name}"
                )
                sources[key] = _combine_sources(
                    sources.get(key) if base_message_calls else None,
                    profile_source if profile_message_calls else None,
                )
            else:
                sources[key] = profile_sources.get(f"extract.{key}", f"профиль: {profile_name}")
        return settings, sources

    def as_mapping(self, profile_name: str | None = None) -> dict[str, Any]:
        settings, sources = self.settings_for_profile(profile_name)
        scan_values = asdict(settings)
        scan = {key: {"value": value, "source": sources[key]} for key, value in scan_values.items()}
        extract_settings, extract_sources = self.extract_settings_for_profile(profile_name)
        extract = {
            "header_language": {
                "value": extract_settings.header_language,
                "source": extract_sources.get("header_language", "по умолчанию"),
            },
            "message_calls": {
                "value": list(extract_settings.message_calls),
                "source": extract_sources.get("message_calls", "по умолчанию"),
            },
            "selectors": {
                extension: {
                    "value": list(patterns),
                    "source": extract_sources.get(f"selectors.{extension}", "по умолчанию"),
                }
                for extension, patterns in sorted(extract_settings.selector_map().items())
            },
        }
        profiles: dict[str, Any] = {}
        for name, profile in sorted(self.profiles.items()):
            profile_extract: dict[str, Any] = {}
            if "header_language" in profile.extract_overrides:
                profile_extract["header_language"] = {
                    "value": profile.extract_overrides["header_language"],
                    "source": self.profile_sources.get(name, {}).get(
                        "extract.header_language", f"профиль: {name}"
                    ),
                }
            if "message_calls" in profile.extract_overrides:
                profile_extract["message_calls"] = {
                    "value": list(profile.extract_overrides["message_calls"]),
                    "source": self.profile_sources.get(name, {}).get(
                        "extract.message_calls", f"профиль: {name}"
                    ),
                }
            if "selectors" in profile.extract_overrides:
                profile_extract["selectors"] = {
                    extension: {
                        "value": list(patterns),
                        "source": self.profile_sources.get(name, {}).get(
                            f"extract.selectors.{extension}", f"профиль: {name}"
                        ),
                    }
                    for extension, patterns in sorted(
                        profile.extract_overrides["selectors"].items()
                    )
                }
            profiles[name] = {
                "priority": {
                    "value": profile.priority,
                    "source": self.profile_sources.get(name, {}).get("priority", "по умолчанию"),
                },
                "project_markers": {
                    "value": list(profile.project_markers),
                    "source": self.profile_sources.get(name, {}).get(
                        "project_markers", "по умолчанию"
                    ),
                },
                "match_paths": {
                    "value": list(profile.match_paths),
                    "source": self.profile_sources.get(name, {}).get("match_paths", "по умолчанию"),
                },
                "scan": {
                    key: {
                        "value": value,
                        "source": self.profile_sources.get(name, {}).get(key, f"профиль: {name}"),
                    }
                    for key, value in sorted(profile.scan_overrides.items())
                },
                "extract": profile_extract,
            }
        return {
            "schema_version": "1.0",
            "selected_profile": profile_name,
            "default_profile": {
                "value": self.default_profile,
                "source": self.default_profile_source,
            },
            "config_files": {
                "user": str(self.user_config_path),
                "explicit": (
                    str(self.explicit_config_path)
                    if self.explicit_config_path is not None
                    else None
                ),
                "loaded": [str(path) for path in self.loaded_config_paths],
            },
            "scan": scan,
            "extract": extract,
            "profiles": profiles,
            "doctor": {
                "programs": list(self.doctor_programs),
                "http_services": {
                    name: redact_url(url) for name, url in sorted(self.doctor_http_services.items())
                },
                "sources": dict(sorted(self.doctor_sources.items())),
            },
        }


def user_config_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "language-review" / "config.toml"
        return Path.home() / "AppData" / "Roaming" / "language-review" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "language-review" / "config.toml"
    return Path.home() / ".config" / "language-review" / "config.toml"


def _as_string_list(value: Any, key: str, *, path_patterns: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"Параметр {key} должен быть списком строк.")
    cleaned: list[str] = []
    for raw_item in value:
        item = raw_item.strip()
        if not item:
            raise ConfigurationError(f"Параметр {key} не может содержать пустые строки.")
        if path_patterns and (Path(item).is_absolute() or ".." in Path(item).parts):
            raise ConfigurationError(f"Шаблон {key} должен оставаться внутри корня.")
        cleaned.append(item)
    return tuple(cleaned)


def _validate_scan(raw: Any, label: str, *, partial: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Раздел {label} должен быть таблицей TOML.")
    unknown = set(raw) - SCAN_KEYS
    if unknown:
        raise ConfigurationError(f"Неизвестные параметры в {label}: {', '.join(sorted(unknown))}.")
    checked: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "extensions":
            items = _as_string_list(value, f"{label}.{key}")
            normalized: list[str] = []
            for item in items:
                extension = item.lower()
                if not extension.startswith(".") or any(char in extension for char in "/\\*?"):
                    raise ConfigurationError(
                        f"Расширение {item!r} в {label}.{key} должно начинаться с точки."
                    )
                normalized.append(extension)
            checked[key] = tuple(dict.fromkeys(normalized))
        elif key == "exclude_dirs":
            items = _as_string_list(value, f"{label}.{key}")
            if any("/" in item or "\\" in item or item in {".", ".."} for item in items):
                raise ConfigurationError(f"{label}.{key} должен содержать только имена каталогов.")
            checked[key] = tuple(dict.fromkeys(items))
        elif key == "exclude_patterns":
            items = _as_string_list(value, f"{label}.{key}", path_patterns=True)
            if any(item.startswith("!") for item in items):
                raise ConfigurationError(
                    f"{label}.{key} содержит исключения без отрицательных правил; "
                    "для .gitignore используйте его собственный синтаксис."
                )
            checked[key] = items
        elif key in {"respect_gitignore", "follow_symlinks"}:
            if not isinstance(value, bool):
                raise ConfigurationError(f"{label}.{key} должен быть логическим значением.")
            checked[key] = value
        elif key == "max_file_size":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigurationError(f"{label}.{key} должен быть положительным целым числом.")
            checked[key] = value
    if not partial:
        defaults = asdict(ScanSettings())
        defaults.update(checked)
        return defaults
    return checked


def _validate_selector(selector: str, label: str) -> str:
    if not selector.startswith("$"):
        raise ConfigurationError(f"Селектор {label} должен начинаться с символа $.")
    index = 1
    while index < len(selector):
        if selector[index] == ".":
            index += 1
            start = index
            while index < len(selector) and (
                selector[index].isalnum() or selector[index] in "_-*@:"
            ):
                index += 1
            if start == index:
                raise ConfigurationError(f"Некорректный селектор {label}: {selector!r}.")
            continue
        if selector[index] == "[":
            closing = selector.find("]", index + 1)
            if closing < 0:
                raise ConfigurationError(f"Некорректный селектор {label}: {selector!r}.")
            item = selector[index + 1 : closing]
            if not (item == "*" or item.isdecimal()):
                if len(item) < 2 or item[0] not in "\"'" or item[-1] != item[0]:
                    raise ConfigurationError(f"Некорректный селектор {label}: {selector!r}.")
            index = closing + 1
            continue
        raise ConfigurationError(f"Некорректный селектор {label}: {selector!r}.")
    return selector


def _validate_extract(raw: Any, label: str, *, partial: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Раздел {label} должен быть таблицей TOML.")
    unknown = set(raw) - EXTRACT_KEYS
    if unknown:
        raise ConfigurationError(f"Неизвестные параметры в {label}: {', '.join(sorted(unknown))}.")
    checked: dict[str, Any] = {}
    if "header_language" in raw:
        value = raw["header_language"]
        if not isinstance(value, str) or value not in {"auto", "c", "cpp"}:
            raise ConfigurationError(f"{label}.header_language должен быть auto, c или cpp.")
        checked["header_language"] = value
    if "message_calls" in raw:
        calls = _as_string_list(raw["message_calls"], f"{label}.message_calls")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.$:!-]*", name) for name in calls):
            raise ConfigurationError(f"{label}.message_calls содержит некорректное имя вызова.")
        checked["message_calls"] = tuple(dict.fromkeys(calls))
    if "selectors" in raw:
        raw_selectors = raw["selectors"]
        if not isinstance(raw_selectors, dict):
            raise ConfigurationError(f"{label}.selectors должен быть таблицей расширений.")
        selectors: dict[str, tuple[str, ...]] = {}
        for extension, values in raw_selectors.items():
            if not isinstance(extension, str):
                raise ConfigurationError(f"{label}.selectors содержит неверное расширение.")
            normalized = extension.lower()
            if normalized not in SELECTOR_EXTENSIONS:
                raise ConfigurationError(
                    f"{label}.selectors не поддерживает расширение {extension!r}."
                )
            patterns = _as_string_list(values, f"{label}.selectors.{extension}")
            selectors[normalized] = tuple(
                dict.fromkeys(
                    _validate_selector(pattern, f"{label}.selectors.{extension}")
                    for pattern in patterns
                )
            )
        checked["selectors"] = selectors
    if not partial:
        defaults = asdict(ExtractSettings())
        defaults.update(checked)
        return defaults
    return checked


def _validate_doctor(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Раздел {label} должен быть таблицей TOML.")
    unknown = set(raw) - DOCTOR_KEYS
    if unknown:
        raise ConfigurationError(f"Неизвестные параметры в {label}: {', '.join(sorted(unknown))}.")
    checked: dict[str, Any] = {}
    if "programs" in raw:
        programs = _as_string_list(raw["programs"], f"{label}.programs")
        if any("\x00" in program or "\n" in program for program in programs):
            raise ConfigurationError(f"{label}.programs содержит недопустимый путь.")
        checked["programs"] = tuple(dict.fromkeys(programs))
    if "http_services" in raw:
        services = raw["http_services"]
        if not isinstance(services, dict) or any(
            not isinstance(name, str) or not isinstance(url, str) for name, url in services.items()
        ):
            raise ConfigurationError(f"{label}.http_services должен быть таблицей URL.")
        validated: dict[str, str] = {}
        for name, url in services.items():
            if not name.strip():
                raise ConfigurationError(f"{label}.http_services содержит пустое имя.")
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ConfigurationError(
                    f"URL сервиса {name!r} должен использовать HTTP или HTTPS."
                )
            if parts.hostname.lower() != "localhost":
                try:
                    if not ipaddress.ip_address(parts.hostname).is_loopback:
                        raise ConfigurationError(
                            f"Сервис {name!r} должен быть адресован loopback-интерфейсу."
                        )
                except ValueError as exc:
                    raise ConfigurationError(
                        f"Сервис {name!r} должен быть адресован loopback-интерфейсу."
                    ) from exc
            try:
                port = parts.port
            except ValueError as exc:
                raise ConfigurationError(f"В URL сервиса {name!r} указан неверный порт.") from exc
            if port == 0:
                raise ConfigurationError(f"В URL сервиса {name!r} указан неверный порт.")
            if parts.fragment:
                raise ConfigurationError(f"URL сервиса {name!r} не должен содержать fragment.")
            validated[name] = url
        checked["http_services"] = validated
    return checked


def _read_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigurationError(f"Не удалось прочитать конфигурацию {path}: {exc}.") from exc
    except UnicodeError as exc:
        raise ConfigurationError(f"Конфигурация {path} должна быть в UTF-8: {exc}.") from exc
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Ошибка TOML в {path}: {exc}.") from exc
    if not isinstance(document, dict):
        raise ConfigurationError(f"Корень конфигурации {path} должен быть таблицей.")
    unknown = set(document) - ROOT_KEYS
    if unknown:
        raise ConfigurationError(f"Неизвестные разделы в {path}: {', '.join(sorted(unknown))}.")
    return document


def _profile_definitions(
    raw: Any, path: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Раздел profiles в {path} должен быть таблицей.")
    definitions: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, str]] = {}
    for name, value in raw.items():
        if not PROFILE_NAME.fullmatch(name):
            raise ConfigurationError(f"Недопустимое имя профиля: {name!r}.")
        if not isinstance(value, dict):
            raise ConfigurationError(f"Профиль {name!r} должен быть таблицей.")
        unknown = set(value) - PROFILE_KEYS
        if unknown:
            raise ConfigurationError(
                f"Неизвестные параметры профиля {name!r}: {', '.join(sorted(unknown))}."
            )
        definition: dict[str, Any] = {}
        sources[name] = {
            key: f"профиль {name} из {path}"
            for key in ("priority", "project_markers", "match_paths")
            if key in value
        }
        if "extract" in value:
            definition["extract"] = _validate_extract(
                value["extract"], f"profiles.{name}.extract", partial=True
            )
            for key, item in definition["extract"].items():
                if key == "selectors":
                    sources[name].update(
                        {
                            f"extract.selectors.{extension}": f"профиль {name} из {path}"
                            for extension in item
                        }
                    )
                else:
                    sources[name][f"extract.{key}"] = f"профиль {name} из {path}"
        if "priority" in value:
            priority = value["priority"]
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise ConfigurationError(f"Приоритет профиля {name!r} должен быть целым числом.")
            definition["priority"] = priority
        for selector in ("project_markers", "match_paths"):
            if selector in value:
                definition[selector] = _as_string_list(
                    value[selector], f"profiles.{name}.{selector}", path_patterns=True
                )
        if "scan" in value:
            definition["scan"] = _validate_scan(
                value["scan"], f"profiles.{name}.scan", partial=True
            )
            sources[name].update({key: f"профиль {name} из {path}" for key in definition["scan"]})
        definitions[name] = definition
    return definitions, sources


def _merge_file(
    path: Path,
    scan: dict[str, Any],
    scan_sources: dict[str, str],
    extract: dict[str, Any],
    extract_sources: dict[str, str],
    profiles: dict[str, dict[str, Any]],
    profile_sources: dict[str, dict[str, str]],
    doctor: dict[str, Any],
    doctor_sources: dict[str, str],
    state: dict[str, Any],
) -> None:
    document = _read_document(path)
    label = f"конфигурация {path}"
    if "scan" in document:
        values = _validate_scan(document["scan"], f"{label}.scan", partial=True)
        scan.update(values)
        scan_sources.update({key: label for key in values})
    if "extract" in document:
        values = _validate_extract(document["extract"], f"{label}.extract", partial=True)
        for key, value in values.items():
            if key == "selectors":
                selectors = extract.setdefault("selectors", {})
                selectors.update(value)
                extract_sources.update({f"selectors.{extension}": label for extension in value})
            else:
                extract[key] = value
                extract_sources[key] = label
    if "default_profile" in document:
        profile_name = document["default_profile"]
        if not isinstance(profile_name, str) or not PROFILE_NAME.fullmatch(profile_name):
            raise ConfigurationError(f"default_profile в {path} должен быть именем профиля.")
        state["default_profile"] = profile_name
        state["default_profile_source"] = label
    if "profiles" in document:
        new_profiles, new_sources = _profile_definitions(document["profiles"], path)
        for name, incoming in new_profiles.items():
            target = profiles.setdefault(name, {})
            target.update(
                {key: value for key, value in incoming.items() if key not in {"scan", "extract"}}
            )
            target.setdefault("priority", 0)
            target.setdefault("project_markers", ())
            target.setdefault("match_paths", ())
            if "scan" in incoming:
                target.setdefault("scan", {}).update(incoming["scan"])
            if "extract" in incoming:
                extract_target = target.setdefault("extract", {})
                for key, value in incoming["extract"].items():
                    if key == "selectors":
                        extract_target.setdefault("selectors", {}).update(value)
                    elif key == "message_calls":
                        extract_target[key] = tuple(
                            dict.fromkeys((*extract_target.get(key, ()), *value))
                        )
                    else:
                        extract_target[key] = value
            source_target = profile_sources.setdefault(name, {})
            for key, source in new_sources.get(name, {}).items():
                if key == "extract.message_calls":
                    source_target[key] = _combine_sources(source_target.get(key), source)
                else:
                    source_target[key] = source
    if "doctor" in document:
        values = _validate_doctor(document["doctor"], f"{label}.doctor")
        doctor.update(values)
        doctor_sources.update({key: label for key in values})
    state["loaded"].append(path)


def load_config(explicit_path: str | Path | None = None) -> LoadedConfig:
    user_path = user_config_path()
    scan = asdict(ScanSettings())
    scan_sources = {key: "по умолчанию" for key in scan}
    extract = asdict(ExtractSettings())
    extract_sources = {
        "header_language": "по умолчанию",
        "message_calls": "по умолчанию",
    }
    profiles: dict[str, dict[str, Any]] = {}
    profile_sources: dict[str, dict[str, str]] = {}
    doctor: dict[str, Any] = {"programs": (), "http_services": {}}
    doctor_sources = {"programs": "по умолчанию", "http_services": "по умолчанию"}
    state: dict[str, Any] = {
        "loaded": [],
        "default_profile": None,
        "default_profile_source": "по умолчанию",
    }

    if user_path.is_file():
        _merge_file(
            user_path,
            scan,
            scan_sources,
            extract,
            extract_sources,
            profiles,
            profile_sources,
            doctor,
            doctor_sources,
            state,
        )
    elif user_path.exists():
        raise ConfigurationError(
            f"Путь пользовательской конфигурации не является файлом: {user_path}."
        )

    resolved_explicit: Path | None = None
    if explicit_path is not None:
        resolved_explicit = Path(explicit_path).expanduser().resolve()
        if not resolved_explicit.is_file():
            raise ConfigurationError(f"Файл конфигурации не найден: {resolved_explicit}.")
        _merge_file(
            resolved_explicit,
            scan,
            scan_sources,
            extract,
            extract_sources,
            profiles,
            profile_sources,
            doctor,
            doctor_sources,
            state,
        )

    default_profile = state["default_profile"]
    if default_profile is not None and default_profile not in profiles:
        raise ConfigurationError(
            f"Профиль по умолчанию {default_profile!r} не определён ни в одном конфиге."
        )
    compiled_profiles: dict[str, Profile] = {}
    for name, raw in profiles.items():
        compiled_profiles[name] = Profile(
            name=name,
            priority=raw["priority"],
            project_markers=raw["project_markers"],
            match_paths=raw["match_paths"],
            scan_overrides=dict(raw.get("scan", {})),
            extract_overrides=dict(raw.get("extract", {})),
        )

    return LoadedConfig(
        scan=ScanSettings(**scan),
        extract=ExtractSettings(**extract),
        sources=scan_sources,
        extract_sources=extract_sources,
        profiles=compiled_profiles,
        profile_sources=profile_sources,
        default_profile=default_profile,
        default_profile_source=state["default_profile_source"],
        doctor_programs=tuple(doctor["programs"]),
        doctor_http_services=dict(doctor["http_services"]),
        doctor_sources=doctor_sources,
        user_config_path=user_path,
        explicit_config_path=resolved_explicit,
        loaded_config_paths=tuple(state["loaded"]),
    )


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if parts.username is not None or parts.password is not None:
        host = f"***@{host}"
    query = "***" if parts.query else ""
    safe = SplitResult(parts.scheme, host, parts.path, query, "")
    return urlunsplit(safe)
