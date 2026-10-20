from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from language_review import __version__
from language_review.config import LoadedConfig, redact_url

if TYPE_CHECKING:
    from language_review.check import EngineFactory


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _check_path(raw: str) -> dict[str, Any]:
    path = Path(raw).expanduser().absolute()
    try:
        metadata = path.stat()
        if path.is_dir():
            with os.scandir(path) as entries:
                next(entries, None)
            kind = "directory"
        elif path.is_file():
            with path.open("rb") as stream:
                stream.read(0)
            kind = "file"
        else:
            return {
                "path": str(path),
                "status": "unsupported",
                "error": "не обычный файл или каталог",
            }
    except FileNotFoundError:
        return {"path": str(path), "status": "missing", "error": "путь не найден"}
    except OSError as exc:
        return {
            "path": str(path),
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "path": str(path),
        "status": "available",
        "kind": kind,
        "size_bytes": metadata.st_size if kind == "file" else None,
    }


def _check_program(name: str) -> dict[str, Any]:
    resolved = shutil.which(name)
    if resolved is None:
        return {"name": name, "status": "missing"}
    return {"name": name, "status": "available", "path": os.path.abspath(resolved)}


def _check_http(name: str, url: str) -> dict[str, Any]:
    safe_url = redact_url(url)
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"language-review-doctor/{__version__}"},
    )
    try:
        with opener.open(request, timeout=2.0) as response:
            status_code = response.status
    except urllib.error.HTTPError as exc:
        status_code = exc.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "name": name,
            "url": safe_url,
            "status": "unavailable",
            "error": type(exc).__name__,
        }
    status = "reachable" if status_code < 500 else "unhealthy"
    return {
        "name": name,
        "url": safe_url,
        "status": status,
        "http_status": status_code,
    }


def _check_engines(
    config: LoadedConfig, engine_factory: EngineFactory | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from language_review.check import ENGINE_ORDER, default_engine_factory

    factory = engine_factory or default_engine_factory
    settings, sources = config.check_settings_for_profile(None)
    engines: list[dict[str, Any]] = []
    for name in ENGINE_ORDER:
        if name not in settings.engines:
            continue
        engine = factory(name, settings)
        try:
            details = engine.describe()
            probe = engine.probe()
        finally:
            engine.close()
        details.update(probe.details)
        engines.append(
            {
                "name": name,
                "status": probe.status,
                "details": dict(sorted(details.items())),
                "diagnostics": list(probe.diagnostics),
                "message": probe.message,
            }
        )
    check = {
        "engines": list(settings.engines),
        "categories": list(settings.categories),
        "llm_provider": settings.llm_provider,
        "allow_remote_endpoints": settings.allow_remote_endpoints,
        "sources": {
            key: sources.get(key, "по умолчанию")
            for key in ("engines", "categories", "llm.provider", "allow_remote_endpoints")
        },
    }
    return check, engines


def run_doctor(
    config: LoadedConfig, paths: list[str], *, engine_factory: EngineFactory | None = None
) -> dict[str, Any]:
    try:
        package_version = importlib.metadata.version("language-review")
    except importlib.metadata.PackageNotFoundError:
        package_version = __version__
        package_status = "source_checkout"
    else:
        package_status = "installed"

    path_checks = [_check_path(raw) for raw in paths]
    program_checks = [_check_program(name) for name in config.doctor_programs]
    http_checks = [
        _check_http(name, url) for name, url in sorted(config.doctor_http_services.items())
    ]
    check_settings, engine_checks = _check_engines(config, engine_factory)
    memory: dict[str, Any] = {"status": "disabled"}
    if config.memory.enabled:
        from language_review.memory.store import MemoryFeatureError, Store

        try:
            memory = Store(config.memory).health()
        except MemoryFeatureError as exc:
            memory = {"status": "unavailable", "reason": str(exc)}
    failures = (
        any(check["status"] != "available" for check in program_checks)
        or any(check["status"] in {"unavailable", "unhealthy"} for check in http_checks)
        or any(check["status"] != "available" for check in engine_checks)
        or memory["status"] == "unavailable"
    )
    input_errors = any(check["status"] != "available" for check in path_checks)
    return {
        "schema_version": "1.0",
        "status": "issues" if failures or input_errors else "ok",
        "python": {
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "executable": sys.executable,
        },
        "package": {
            "name": "language-review",
            "version": package_version,
            "status": package_status,
        },
        "configuration": {
            "status": "valid",
            "loaded_files": [str(path) for path in config.loaded_config_paths],
        },
        "paths": path_checks,
        "programs": program_checks,
        "http_services": http_checks,
        "check": check_settings,
        "engines": engine_checks,
        "memory": memory,
        "input_errors": input_errors,
        "external_failures": failures,
    }


def doctor_human(result: dict[str, Any]) -> str:
    lines = [f"Диагностика Language Review: {result['status']}"]
    python = result["python"]
    package = result["package"]
    lines.append(f"Python: {python['version']} — {python['executable']}")
    lines.append(f"Пакет: {package['name']} {package['version']} ({package['status']})")
    loaded = result["configuration"]["loaded_files"]
    lines.append(f"Конфигурация: корректна; загружено файлов: {len(loaded)}")
    lines.append(f"Предметная память: {result['memory']['status']}")
    for check in result["paths"]:
        detail = check.get("error") or check.get("kind", "")
        lines.append(
            f"Путь: {check['path']} — {check['status']}" + (f" ({detail})" if detail else "")
        )
    for check in result["programs"]:
        detail = check.get("path", "")
        lines.append(
            f"Программа: {check['name']} — {check['status']}" + (f" ({detail})" if detail else "")
        )
    for check in result["http_services"]:
        detail = check.get("http_status", check.get("error", ""))
        lines.append(
            f"HTTP-сервис: {check['name']} — {check['status']}" + (f" ({detail})" if detail else "")
        )
    check = result["check"]
    lines.append(
        f"Языковая проверка: движки {', '.join(check['engines'])}; "
        f"LLM-провайдер {check['llm_provider']}"
    )
    for engine in result["engines"]:
        details = engine["details"]
        version = (
            details.get("version") or details.get("server_build") or details.get("server_version")
        )
        parts = [
            str(value)
            for value in (version, details.get("model"), details.get("endpoint"), engine["message"])
            if value
        ]
        lines.append(
            f"Движок: {engine['name']} — {engine['status']}"
            + (f" ({'; '.join(parts)})" if parts else "")
        )
    return "\n".join(lines)


def doctor_exit_code(result: dict[str, Any]) -> int:
    if result["input_errors"]:
        return 2
    if result["external_failures"]:
        return 3
    return 0
