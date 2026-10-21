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
from language_review.memory.settings import MemorySettings, validate_memory

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
ROOT_KEYS = {"scan", "extract", "check", "default_profile", "profiles", "doctor", "memory"}
PROFILE_KEYS = {"priority", "project_markers", "match_paths", "scan", "extract", "check"}
DOCTOR_KEYS = {"programs", "http_services"}
CHECK_ENGINES = ("languagetool", "llm", "vale")
CHECK_CATEGORIES = (
    "spelling",
    "grammar",
    "punctuation",
    "style",
    "bureaucratese",
    "terminology",
    "untranslated_english",
    "typography",
    "other",
)
LLM_PROVIDERS = ("llama_cpp", "ollama", "chatgpt")
ANALYSIS_MODES = ("normal", "adaptive", "deep")
ANALYSIS_BUDGET_DEFAULTS = {
    "analysis.max_requests": 100,
    "analysis.max_text_chars": 200_000,
    "analysis.timeout_seconds": 1800.0,
}
ANALYSIS_STRATEGIES = ("grammar", "spelling", "punctuation", "technical", "meaning", "english")
CHATGPT_EFFORTS = {
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
}
LANGUAGETOOL_LEVELS = ("default", "picky")
# Ключи `[check]` в точечной записи TOML; атрибут CheckSettings получается заменой точек на `_`.
CHECK_KEYS = {
    "analysis.mode",
    "analysis.strategies",
    "analysis.verify",
    "analysis.max_passes",
    "analysis.max_requests",
    "analysis.max_text_chars",
    "analysis.max_context_chars",
    "analysis.timeout_seconds",
    "analysis.concurrency",
    "analysis.retries",
    "llm.chatgpt.model",
    "llm.chatgpt.effort",
    "llm.chatgpt.allow_cloud",
    "llm.chatgpt.allow_memory",
    "engines",
    "categories",
    "terms",
    "allow_remote_endpoints",
    "llm.provider",
    "llm.timeout_seconds",
    "llm.batch_chars",
    "llm.batch_items",
    "llm.max_output_tokens",
    "llm.context_tokens",
    "llm.seed",
    "llm.thinking",
    "llm.llama_cpp.endpoint",
    "llm.llama_cpp.model",
    "llm.ollama.endpoint",
    "llm.ollama.model",
    "languagetool.endpoint",
    "languagetool.language",
    "languagetool.level",
    "languagetool.disabled_rules",
    "languagetool.timeout_seconds",
    "vale.program",
    "vale.config",
    "vale.timeout_seconds",
}
CHECK_TABLES = {
    "analysis",
    "llm",
    "llm.chatgpt",
    "llm.llama_cpp",
    "llm.ollama",
    "languagetool",
    "vale",
}
CHECK_ENDPOINT_KEYS = ("llm.llama_cpp.endpoint", "llm.ollama.endpoint", "languagetool.endpoint")
PROFILE_CHECK_KEYS = {"engines", "categories", "terms"}
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Сегменты имени вызова разделяются как в поддерживаемых языках: `.`, `::` и C++ `->`;
# дефис допустим внутри имени команды PowerShell, `!` — только в конце макроса Rust.
_CALL_SEGMENT = r"[A-Za-z_$][A-Za-z0-9_$]*(?:-[A-Za-z0-9_$]+)*"
MESSAGE_CALL_NAME = re.compile(
    rf"[A-Za-z_][A-Za-z0-9_$]*(?:-[A-Za-z0-9_$]+)*(?:(?:\.|::|->){_CALL_SEGMENT})*!?"
)


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
class CheckSettings:
    analysis_mode: str = "normal"
    analysis_strategies: tuple[str, ...] = ANALYSIS_STRATEGIES
    analysis_verify: bool = False
    analysis_max_passes: int = 8
    analysis_max_requests: int | None = None
    analysis_max_text_chars: int | None = None
    analysis_max_context_chars: int = 4000
    analysis_timeout_seconds: float | None = None
    analysis_concurrency: int = 1
    analysis_retries: int = 0
    llm_chatgpt_model: str = "gpt-6-luna"
    llm_chatgpt_effort: str = "medium"
    llm_chatgpt_allow_cloud: bool = False
    llm_chatgpt_allow_memory: bool = False
    engines: tuple[str, ...] = CHECK_ENGINES
    categories: tuple[str, ...] = ()
    terms: dict[str, str] = field(default_factory=dict)
    allow_remote_endpoints: bool = False
    llm_provider: str = "llama_cpp"
    llm_timeout_seconds: float = 180.0
    llm_batch_chars: int = 1500
    llm_batch_items: int = 8
    llm_max_output_tokens: int = 2048
    llm_context_tokens: int = 8192
    llm_seed: int = 0
    llm_thinking: bool = False
    llm_llama_cpp_endpoint: str = "http://127.0.0.1:8080"
    llm_llama_cpp_model: str = ""
    llm_ollama_endpoint: str = "http://127.0.0.1:11434"
    llm_ollama_model: str = "gemma4:e4b"
    languagetool_endpoint: str = "http://127.0.0.1:8081"
    languagetool_language: str = "ru-RU"
    languagetool_level: str = "default"
    languagetool_disabled_rules: tuple[str, ...] = ()
    languagetool_timeout_seconds: float = 30.0
    vale_program: str = "vale"
    vale_config: str = ""
    vale_timeout_seconds: float = 60.0

    def value(self, key: str) -> Any:
        value = getattr(self, key.replace(".", "_"))
        # Отсутствие настройки сохраняется при смене режима или провайдера через CLI.
        if (
            value is None
            and key in ANALYSIS_BUDGET_DEFAULTS
            and (self.analysis_mode != "normal" or self.llm_provider == "chatgpt")
        ):
            return ANALYSIS_BUDGET_DEFAULTS[key]
        return value


@dataclass(frozen=True)
class Profile:
    name: str
    priority: int
    project_markers: tuple[str, ...]
    match_paths: tuple[str, ...]
    scan_overrides: dict[str, Any]
    extract_overrides: dict[str, Any]
    check_overrides: dict[str, Any] = field(default_factory=dict)


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
    check: CheckSettings
    check_sources: dict[str, str]
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
    memory: MemorySettings = field(default_factory=MemorySettings)
    memory_sources: dict[str, str] = field(default_factory=dict)

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

    def check_settings_for_profile(
        self, profile_name: str | None, cli_overrides: dict[str, Any] | None = None
    ) -> tuple[CheckSettings, dict[str, str]]:
        """Вернуть параметры языковой проверки: база, профиль и затем аргументы CLI."""
        settings = self.check
        sources = dict(self.check_sources)
        if profile_name is not None:
            profile = self.profiles.get(profile_name)
            if profile is None:
                raise ConfigurationError(f"Профиль «{profile_name}» не найден.")
            overrides = dict(profile.check_overrides)
            if "terms" in overrides:
                overrides["terms"] = {**settings.terms, **overrides["terms"]}
            settings = replace(settings, **overrides)
            profile_sources = self.profile_sources.get(profile_name, {})
            for key in profile.check_overrides:
                profile_source = profile_sources.get(f"check.{key}", f"профиль: {profile_name}")
                sources[key] = (
                    _combine_sources(sources.get(key) if self.check.terms else None, profile_source)
                    if key == "terms"
                    else profile_source
                )
        for key, value in (cli_overrides or {}).items():
            if value is None:
                continue
            value = validate_check_value(
                key, list(value) if isinstance(value, tuple) else value, "check"
            )
            settings = replace(settings, **{key.replace(".", "_"): value})
            sources[key] = "аргумент командной строки"
        validate_check_settings(settings)
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
        check_settings, check_sources = self.check_settings_for_profile(profile_name)
        check = {
            key: {
                "value": _check_display_value(key, check_settings.value(key)),
                "source": check_sources.get(key, "по умолчанию"),
            }
            for key in sorted(CHECK_KEYS)
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
                "check": {
                    key: {
                        "value": _check_display_value(key, value),
                        "source": self.profile_sources.get(name, {}).get(
                            f"check.{key}", f"профиль: {name}"
                        ),
                    }
                    for key, value in sorted(profile.check_overrides.items())
                },
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
            "check": check,
            "memory": {
                key: {"value": value, "source": self.memory_sources.get(key, "по умолчанию")}
                for key, value in self.memory.public().items()
            },
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
        if any(not MESSAGE_CALL_NAME.fullmatch(name) for name in calls):
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


def _check_display_value(key: str, value: Any) -> Any:
    if key in CHECK_ENDPOINT_KEYS:
        return redact_url(str(value))
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return dict(sorted(value.items()))
    return value


def is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_service_url(name: str, url: str, *, require_loopback: bool) -> str:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        # urlsplit отклоняет незакрытые скобки и неверный IPv6-литерал в адресе узла.
        raise ConfigurationError(f"URL сервиса {name!r} содержит неверный адрес узла.") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigurationError(f"URL сервиса {name!r} должен использовать HTTP или HTTPS.")
    if require_loopback and not is_loopback_host(parts.hostname):
        raise ConfigurationError(f"Сервис {name!r} должен быть адресован loopback-интерфейсу.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError(f"В URL сервиса {name!r} указан неверный порт.") from exc
    if port == 0:
        raise ConfigurationError(f"В URL сервиса {name!r} указан неверный порт.")
    if parts.fragment:
        raise ConfigurationError(f"URL сервиса {name!r} не должен содержать fragment.")
    return url


def _flatten_check(raw: Any, label: str, prefix: str = "") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Раздел {label} должен быть таблицей TOML.")
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        dotted = f"{prefix}{key}"
        if dotted in CHECK_TABLES:
            flat.update(_flatten_check(value, f"{label}.{key}", f"{dotted}."))
        elif dotted in CHECK_KEYS:
            flat[dotted] = value
        else:
            raise ConfigurationError(f"Неизвестный параметр {label}.{key}.")
    return flat


def _choice_list(value: Any, key: str, choices: tuple[str, ...]) -> tuple[str, ...]:
    items = tuple(dict.fromkeys(_as_string_list(value, key)))
    unknown = [item for item in items if item not in choices]
    if unknown:
        raise ConfigurationError(
            f"{key} содержит неизвестные значения: {', '.join(unknown)}; "
            f"допустимо: {', '.join(choices)}."
        )
    return items


def validate_check_value(key: str, value: Any, label: str, base: Path | None = None) -> Any:
    """Проверить одно значение `[check]`; относительные пути разрешаются от каталога конфига."""
    name = f"{label}.{key}"
    if key == "engines":
        engines = _choice_list(value, name, CHECK_ENGINES)
        if not engines:
            raise ConfigurationError(f"{name} должен содержать хотя бы один движок.")
        return engines
    if key == "categories":
        return _choice_list(value, name, CHECK_CATEGORIES)
    if key == "terms":
        if not isinstance(value, dict):
            raise ConfigurationError(f"{name} должен быть таблицей «термин = замена».")
        terms: dict[str, str] = {}
        for term, replacement in value.items():
            if (
                not isinstance(term, str)
                or not isinstance(replacement, str)
                or not term.strip()
                or not replacement.strip()
                or any(char in term + replacement for char in "\r\n\x00")
            ):
                raise ConfigurationError(f"{name} должен сопоставлять непустые строки.")
            terms[term.strip()] = replacement.strip()
        return terms
    if key in {
        "allow_remote_endpoints",
        "llm.thinking",
        "analysis.verify",
        "llm.chatgpt.allow_cloud",
        "llm.chatgpt.allow_memory",
    }:
        if not isinstance(value, bool):
            raise ConfigurationError(f"{name} должен быть логическим значением.")
        return value
    choices = {
        "analysis.mode": ANALYSIS_MODES,
        "llm.chatgpt.model": tuple(CHATGPT_EFFORTS),
        "llm.chatgpt.effort": ("none", "low", "medium", "high", "xhigh", "max"),
    }
    if key == "analysis.strategies":
        return _choice_list(value, name, ANALYSIS_STRATEGIES)
    if key in choices:
        if not isinstance(value, str) or value not in choices[key]:
            raise ConfigurationError(f"{name}: допустимы {', '.join(choices[key])}.")
        return value
    if key == "llm.provider":
        if value not in LLM_PROVIDERS:
            raise ConfigurationError(f"{name} должен быть одним из: {', '.join(LLM_PROVIDERS)}.")
        return value
    if key == "languagetool.level":
        if value not in LANGUAGETOOL_LEVELS:
            raise ConfigurationError(f"{name} должен быть default или picky.")
        return value
    if key.endswith("timeout_seconds"):
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 3600:
            raise ConfigurationError(f"{name} должен быть числом секунд от 0 до 3600.")
        return float(value)
    limits = {
        "analysis.max_passes": (1, 8),
        "analysis.max_requests": (1, 100_000),
        "analysis.max_text_chars": (1, 100_000_000),
        "analysis.max_context_chars": (0, 10_000),
        "analysis.concurrency": (1, 16),
        "analysis.retries": (0, 3),
        "llm.batch_chars": (200, 50_000),
        "llm.batch_items": (1, 64),
        "llm.max_output_tokens": (64, 32_768),
        "llm.context_tokens": (512, 1_048_576),
        "llm.seed": (0, 2**31 - 1),
    }
    if key in limits:
        low, high = limits[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigurationError(f"{name} должен быть целым числом от {low} до {high}.")
        return value
    if key == "languagetool.disabled_rules":
        rules = tuple(dict.fromkeys(_as_string_list(value, name)))
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", rule) for rule in rules):
            raise ConfigurationError(f"{name} содержит некорректный идентификатор правила.")
        return rules
    if not isinstance(value, str) or "\x00" in value or "\n" in value:
        raise ConfigurationError(f"{name} должен быть строкой.")
    value = value.strip()
    if key in CHECK_ENDPOINT_KEYS:
        return _validate_service_url(key, value, require_loopback=False)
    if key in {"llm.ollama.model", "languagetool.language", "vale.program"} and not value:
        raise ConfigurationError(f"{name} не может быть пустым.")
    if key in {"vale.config", "vale.program"} and value and base is not None:
        candidate = Path(value).expanduser()
        if key == "vale.config" or len(candidate.parts) > 1:
            if not candidate.is_absolute():
                candidate = base / candidate
            return str(candidate.resolve())
    return value


def validate_check_settings(settings: CheckSettings) -> None:
    if settings.llm_provider == "chatgpt":
        efforts = CHATGPT_EFFORTS.get(settings.llm_chatgpt_model, ())
        if settings.llm_chatgpt_effort not in efforts:
            raise ConfigurationError("Несовместимые ChatGPT model/effort; выберите их явно.")


def _validate_check(
    raw: Any, label: str, *, base: Path | None, profile: bool = False
) -> dict[str, Any]:
    flat = _flatten_check(raw, label)
    if profile:
        unknown = set(flat) - PROFILE_CHECK_KEYS
        if unknown:
            raise ConfigurationError(
                f"Профиль может переопределять в {label} только engines, categories и terms; "
                f"найдено: {', '.join(sorted(unknown))}."
            )
    return {key: validate_check_value(key, value, label, base) for key, value in flat.items()}


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
            validated[name] = _validate_service_url(name, url, require_loopback=True)
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
        if "check" in value:
            definition["check"] = _validate_check(
                value["check"], f"profiles.{name}.check", base=path.parent, profile=True
            )
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
        sources[name].update(
            {f"check.{key}": f"профиль {name} из {path}" for key in definition.get("check", {})}
        )
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
    check: dict[str, Any],
    check_sources: dict[str, str],
    profiles: dict[str, dict[str, Any]],
    profile_sources: dict[str, dict[str, str]],
    doctor: dict[str, Any],
    doctor_sources: dict[str, str],
    state: dict[str, Any],
) -> None:
    document = _read_document(path)
    label = f"конфигурация {path}"
    if "memory" in document:
        values = validate_memory(document["memory"])
        state.setdefault("memory", {}).update(values)
        state.setdefault("memory_sources", {}).update({key: label for key in values})
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
    if "check" in document:
        values = _validate_check(document["check"], f"{label}.check", base=path.parent)
        for key, value in values.items():
            if key == "terms":
                check[key] = {**check.get(key, {}), **value}
            else:
                check[key] = value
            check_sources[key] = (
                _combine_sources(check_sources.get(key), label) if key == "terms" else label
            )
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
                {
                    key: value
                    for key, value in incoming.items()
                    if key not in {"scan", "extract", "check"}
                }
            )
            target.setdefault("priority", 0)
            target.setdefault("project_markers", ())
            target.setdefault("match_paths", ())
            if "scan" in incoming:
                target.setdefault("scan", {}).update(incoming["scan"])
            if "check" in incoming:
                check_target = target.setdefault("check", {})
                for key, value in incoming["check"].items():
                    if key == "terms":
                        check_target[key] = {**check_target.get(key, {}), **value}
                    else:
                        check_target[key] = value
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
                if key in {"extract.message_calls", "check.terms"}:
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
    check: dict[str, Any] = {}
    check_sources = {key: "по умолчанию" for key in CHECK_KEYS}
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
            check,
            check_sources,
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
            check,
            check_sources,
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
    check_settings = replace(
        CheckSettings(), **{key.replace(".", "_"): value for key, value in check.items()}
    )
    if not check_settings.allow_remote_endpoints:
        for key in CHECK_ENDPOINT_KEYS:
            if not is_loopback_host(urlsplit(check_settings.value(key)).hostname):
                raise ConfigurationError(
                    f"check.{key} указывает на нелокальный адрес; удалённые сервисы получают "
                    "тексты исходников и включаются только явным "
                    "check.allow_remote_endpoints = true."
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
            check_overrides=dict(raw.get("check", {})),
        )

    return LoadedConfig(
        scan=ScanSettings(**scan),
        extract=ExtractSettings(**extract),
        sources=scan_sources,
        extract_sources=extract_sources,
        check=check_settings,
        memory=MemorySettings(**state.get("memory", {})),
        memory_sources=state.get("memory_sources", {}),
        check_sources=check_sources,
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
