"""Миграция старых синтетических фикстур в явный проектный YAML.

Эти помощники готовят входы, затем вызывают настоящий публичный конвейер.
Тесты нового контракта используют API напрямую, без этой подготовки.
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from language_review.config import CHECK_KEYS, LoadedConfig, load_config
from language_review.errors import ConfigurationError
from language_review.project_config import ENVIRONMENT_KEYS, FILENAME, repository_root

_GENERATED_POLICY_MARKER = "# Синтетическая политика из TOML для тестов."


def _policy(settings: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    provider = settings.get("llm_provider", "llama_cpp")
    for key in sorted(CHECK_KEYS - ENVIRONMENT_KEYS):
        value = settings.get(key.replace(".", "_"))
        if value is None:
            continue
        parts = key.split(".")
        if len(parts) > 2:
            if parts[:2] != ["llm", provider]:
                continue
            if parts[2] == "model" and not value:
                continue
            parts = ["llm", "reasoning_effort" if parts[2] == "effort" else parts[2]]
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = list(value) if isinstance(value, tuple) else value
    return result


def prepare(paths: list[str], config: LoadedConfig) -> None:
    from language_review.scanner import _choose_profile

    checkout = Path(__file__).resolve().parents[2]
    temporary = Path(tempfile.gettempdir()).resolve()
    for raw in paths:
        if not Path(raw).exists():
            continue
        root = repository_root(raw)
        if root == checkout:
            continue
        if not root.is_relative_to(temporary):
            raise AssertionError("Подготовка фикстуры вышла за системный временный каталог.")
        policy_path = root / FILENAME
        regenerate = False
        if policy_path.exists():
            try:
                with policy_path.open("r", encoding="utf-8") as existing:
                    regenerate = existing.readline().rstrip("\r\n") == _GENERATED_POLICY_MARKER
            except OSError, UnicodeError:
                continue
            if not regenerate:
                continue
        profile = _choose_profile(config, root, None) or "fixture"
        definitions = {}
        rules = []
        for name in dict.fromkeys((profile, *config.profiles)):
            settings, _ = config.check_settings_for_profile(
                name if name in config.profiles else None
            )
            scan, _ = config.settings_for_profile(name if name in config.profiles else None)
            extract, _ = config.extract_settings_for_profile(
                name if name in config.profiles else None
            )
            definitions[name] = _policy(asdict(settings)) | {
                "scan": asdict(scan),
                "extract": asdict(extract),
            }
            old = config.profiles.get(name)
            if old:
                for pattern in old.match_paths:
                    patterns = (
                        [pattern]
                        if not pattern.startswith("*.")
                        else [p.relative_to(root).as_posix() for p in root.rglob(pattern)]
                    )
                    rules.extend(
                        {"path": p, "profile": name, "priority": old.priority} for p in patterns
                    )
        document = {
            "schema_version": 1,
            "default_profile": profile,
            "profiles": definitions,
            "path_rules": rules,
        }
        stream = io.StringIO()
        YAML(typ="safe", pure=True).dump(document, stream)
        generated = f"{_GENERATED_POLICY_MARKER}\n{stream.getvalue()}"
        if regenerate:
            policy_path.write_text(generated, encoding="utf-8")
        else:
            try:
                with policy_path.open("x", encoding="utf-8") as target:
                    target.write(generated)
            except FileExistsError:
                continue


def prepare_cli(arguments: list[str], appdata: Path) -> None:
    command = next((name for name in ("scan", "extract", "check") if name in arguments), None)
    if command is None:
        return
    index = arguments.index(command) + 1
    paths = []
    while index < len(arguments) and not arguments[index].startswith("--"):
        paths.append(arguments[index])
        index += 1
    previous = os.environ.get("APPDATA")
    os.environ["APPDATA"] = str(appdata)
    try:
        explicit = arguments[arguments.index("--config") + 1] if "--config" in arguments else None
        try:
            config = load_config(explicit)
        except ConfigurationError:
            return
        prepare(paths, config)
    finally:
        if previous is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = previous


def check_fixture_paths(paths: list[str], config: LoadedConfig, **options: Any) -> Any:
    from language_review.check import check_paths

    prepare(paths, config)
    return check_paths(paths, config, **options)


def extract_fixture_paths(paths: list[str], config: LoadedConfig, **options: Any) -> Any:
    from language_review.extraction import extract_paths

    prepare(paths, config)
    return extract_paths(paths, config, **options)


def scan_fixture_paths(paths: list[str], config: LoadedConfig, **options: Any) -> Any:
    from language_review.scanner import scan_paths

    prepare(paths, config)
    return scan_paths(paths, config, **options)
