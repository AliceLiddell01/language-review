"""Управляемая пользовательская Windows-установка, независимая от checkout."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from language_review.release_contract import ReleaseError, digest, read_json, validate_bundle

OWNER = "language-review.installer/1"


def default_root() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "LanguageReview/application"
    )


def safe_path(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ReleaseError("Управляемый путь не должен проходить через ссылку или junction.")


def atomic(path: Path, content: bytes) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def state(root: Path) -> dict[str, Any]:
    safe_path(root)
    file = root / "installation.json"
    if not file.exists():
        return {}
    data = read_json(file)
    if data.get("owner") != OWNER or data.get("root") != str(root.absolute()):
        raise ReleaseError("Каталог принадлежит другой установке.")
    for name in ("active", "previous"):
        if data.get(name) is not None and not re_identifier(data[name]):
            raise ReleaseError("Повреждённый идентификатор установленного runtime.")
    return data


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    safe_path(root)
    if root.exists() and any(root.iterdir()) and not (root / "installation.json").exists():
        raise ReleaseError("Непустой каталог не принадлежит установщику.")
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "installation.json").exists():
        atomic(
            root / "installation.json", json_bytes({"owner": OWNER, "root": str(root.absolute())})
        )
    lock = root / ".install-lock"
    try:
        stream = lock.open("x")
    except FileExistsError as exc:
        raise ReleaseError(
            "Другая операция установки уже выполняется; проверьте .install-lock."
        ) from exc
    try:
        with stream:
            stream.write(str(os.getpid()))
            stream.flush()
            with launch_locked(root):
                recover(root)
            yield
    finally:
        lock.unlink(missing_ok=True)


@contextlib.contextmanager
def launch_locked(root: Path) -> Iterator[None]:
    """Сериализует выбор active runtime с переключением и очисткой поколений."""
    from importlib import import_module

    msvcrt = cast(Any, import_module("msvcrt"))
    lock = root / ".launch-lock"
    safe_path(lock)
    deadline = time.monotonic() + 30
    # Блокировкой владеет дескриптор, а не pathname. Windows освобождает диапазон
    # после гибели процесса. Оставшийся файл не мешает следующему захвату.
    try:
        with lock.open("a+b") as stream:
            stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ReleaseError("Не удалось дождаться завершения запуска CLI.") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        # Оба клиента запрещают FILE_SHARE_DELETE: Windows не даст удалить файл
        # с открытым чужим дескриптором. Очистка имени также совместима со старыми
        # launcher, ожидающими CreateNew; занятый файл оставляется следующему владельцу.
        with contextlib.suppress(OSError):
            lock.unlink(missing_ok=True)


def _has_live_runtime_lease(release: Path) -> bool:
    lease_directory = release / ".run-leases"
    safe_path(lease_directory)
    if not lease_directory.is_dir():
        return True
    protocol = lease_directory / "protocol"
    try:
        safe_path(release / "owned-files.json")
        index = read_json(release / "owned-files.json")
        expected = index.get("files", {}).get(str(Path(".run-leases") / "protocol"))
        if index.get("owner") != OWNER or not expected or digest(protocol) != expected:
            return True
    except OSError, ReleaseError, TypeError, AttributeError:
        return True
    try:
        leases = list(lease_directory.iterdir())
    except OSError:
        return True
    for lease in leases:
        if lease == protocol:
            continue
        if not lease.is_file() or lease.suffix != ".lock":
            return True
        try:
            from importlib import import_module

            msvcrt = cast(Any, import_module("msvcrt"))
            with lease.open("r+b") as stream:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                else:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            lease.unlink()
        except ImportError, OSError:
            return True
    return False


def _remove_owned_release(release: Path) -> bool:
    safe_path(release)
    index_path = release / "owned-files.json"
    safe_path(index_path)
    try:
        index = read_json(index_path)
    except OSError, ReleaseError:
        return False
    files = index.get("files")
    if index.get("owner") != OWNER or not isinstance(files, dict):
        return False
    owned: list[tuple[Path, str]] = []
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            return False
        normalized = name.replace("\\", "/")
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or (len(normalized) > 1 and normalized[1] == ":")
            or any(part in {"", ".", ".."} for part in parts)
            or len(expected) != 64
        ):
            return False
        path = release.joinpath(*parts)
        safe_path(path)
        owned.append((path, expected))
    complete = True
    for path, expected in owned:
        try:
            if not path.exists():
                continue
            if not path.is_file() or digest(path) != expected:
                complete = False
                continue
            path.unlink()
        except OSError:
            complete = False
    for directory in sorted(
        (path for path in release.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            safe_path(directory)
            directory.rmdir()
        except OSError:
            complete = False
    if not complete:
        return False
    try:
        if any(path != index_path for path in release.rglob("*")):
            return False
        index_path.unlink()
        release.rmdir()
        return True
    except OSError:
        return False


def prune_stale_releases(root: Path, *, active: str, previous: str | None) -> list[str]:
    """Удаляет только проверенные поколения вне active/previous без работающих lease."""
    releases = root / "releases"
    safe_path(releases)
    if not releases.is_dir():
        return []
    keep = {active, previous}
    removed: list[str] = []
    for release in releases.iterdir():
        identifier = release.name
        if not release.is_dir() or not re_identifier(identifier) or identifier in keep:
            continue
        try:
            if _has_live_runtime_lease(release):
                continue
            if _remove_owned_release(release):
                removed.append(identifier)
        except OSError, ReleaseError:
            continue
    return removed


def private_uv(root: Path, current: dict[str, Any]) -> str:
    active = current.get("active")
    if not isinstance(active, str) or not re_identifier(active):
        raise ReleaseError("Установленный runtime не содержит корректный active identifier.")
    release = root / "releases" / active
    expected = release / "tools" / "uv.exe"
    configured = current.get("uv")
    if not isinstance(configured, str) or os.path.normcase(
        str(Path(configured).absolute())
    ) != os.path.normcase(str(expected.absolute())):
        raise ReleaseError("Путь установленного uv не соответствует active runtime.")
    safe_path(expected)
    if not expected.is_file():
        raise ReleaseError("Проверенная копия uv отсутствует в active runtime.")
    try:
        safe_path(release / "owned-files.json")
        index = read_json(release / "owned-files.json")
        files = index.get("files", {})
        recorded = files.get(str(Path("tools") / "uv.exe"))
        if recorded is None:
            recorded = files.get("tools/uv.exe")
        if index.get("owner") != OWNER or recorded != digest(expected):
            raise ReleaseError("Копия uv не совпадает с inventory active runtime.")
    except (OSError, TypeError, AttributeError) as exc:
        raise ReleaseError("Не удалось проверить копию uv active runtime.") from exc
    return str(expected)


def recover(root: Path) -> None:
    journal = root / ".transaction.json"
    if not journal.exists():
        return
    import base64

    data = read_json(journal)
    if data.get("owner") != OWNER:
        raise ReleaseError("Неизвестный журнал установки.")
    for entry in reversed(data["files"]):
        path = Path(entry["path"])
        safe_path(path)
        if path.exists() and digest(path) not in {entry["before_hash"], entry["after_hash"]}:
            raise ReleaseError(
                "Файл изменён вне незавершённой транзакции; требуется ручное разрешение конфликта."
            )
        if entry["before"] is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, base64.b64decode(entry["before"]))
    journal.unlink()


def transaction(root: Path, writes: dict[Path, bytes | None]) -> None:
    import base64
    import hashlib

    entries = []
    for path, content in writes.items():
        safe_path(path)
        before = path.read_bytes() if path.exists() else None
        entries.append(
            {
                "path": str(path),
                "before": base64.b64encode(before).decode() if before is not None else None,
                "before_hash": hashlib.sha256(before).hexdigest() if before is not None else None,
                "after_hash": hashlib.sha256(content).hexdigest() if content is not None else None,
            }
        )
    atomic(root / ".transaction.json", json_bytes({"owner": OWNER, "files": entries}))
    try:
        for path, content in writes.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, content)
        (root / ".transaction.json").unlink()
    except BaseException:
        recover(root)
        raise


def skill_path(agent: str, home: Path) -> Path:
    if agent not in {"codex", "claude"}:
        raise ReleaseError("Неизвестная интеграция.")
    return home / (".agents" if agent == "codex" else ".claude") / "skills/language-review/SKILL.md"


def skill_writes(
    old: dict[str, Any], selected: list[str], content: bytes, home: Path
) -> tuple[dict[Path, bytes | None], dict[str, str]]:
    import hashlib

    writes: dict[Path, bytes | None] = {}
    hashes: dict[str, str] = {}
    for agent in sorted(set(selected) | old.get("skills", {}).keys()):
        path = skill_path(agent, home)
        safe_path(path)
        owned = old.get("skills", {}).get(agent)
        # Любой файл без сохранённого ownership — чужой, даже с совпавшими байтами.
        if path.exists() and (not owned or digest(path) != owned):
            raise ReleaseError(
                "Skill изменён пользователем или принадлежит другой установке: " + agent
            )
        if agent in selected:
            writes[path] = content
            hashes[agent] = hashlib.sha256(content).hexdigest()
        elif path.exists():
            writes[path] = None
    return writes, hashes


def execute(args: list[str], *, expected: set[int] | None = None) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=tempfile.gettempdir(),
    )
    if result.returncode not in (expected or {0}):
        import re

        detail = re.sub(r"(?:https?|file)://\S+", "<адрес>", result.stderr[-2000:])
        raise ReleaseError(
            "Проверка/установка кандидата завершилась ошибкой: "
            + Path(args[0]).name
            + f" (exit {result.returncode}): "
            + detail
        )
    return result.stdout.strip()


def python_at(release: Path) -> Path:
    return release / "venv/Scripts/python.exe"


def probe(release: Path, version: str) -> None:
    python = str(python_at(release))
    if (
        execute([python, "-I", "-m", "language_review", "--version"])
        != "language-review " + version
    ):
        raise ReleaseError("CLI кандидата сообщил другую версию.")
    execute(
        [
            python,
            "-I",
            "-m",
            "language_review",
            "config",
            "show",
            "--environment",
            "--format",
            "json",
        ]
    )
    execute([python, "-I", "-m", "language_review.evaluation", "--help"])
    # Недоступные необязательные движки — допустимый диагностический результат ядра.
    report = json.loads(
        execute(
            [python, "-I", "-m", "language_review", "doctor", "--format", "json"], expected={0, 3}
        )
    )
    if report["package"]["version"] != version or report["configuration"]["status"] != "valid":
        raise ReleaseError("Диагностика кандидата не соответствует поставке.")


def change_path(bin_path: Path, *, add: bool) -> bool:
    if sys.platform != "win32":
        raise ReleaseError("Изменение пользовательского PATH поддерживается только в Windows.")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        # Пустые элементы/последний разделитель чужого PATH сохраняются побайтово.
        parts = value.split(";") if value else []
        present = any(
            os.path.normcase(os.path.expandvars(p)) == os.path.normcase(str(bin_path))
            for p in parts
        )
        if add and not present:
            parts.append(str(bin_path))
        elif not add and present:
            parts = [
                p
                for p in parts
                if os.path.normcase(os.path.expandvars(p)) != os.path.normcase(str(bin_path))
            ]
        else:
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    # Новые терминалы должны унаследовать изменение от оболочки Windows.
    import ctypes

    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x1A, 0, "Environment", 2, 2000, ctypes.byref(result)
    )
    return True


def path_conflict(root: Path) -> None:
    for name in ("language-review", "language-review-eval"):
        found = shutil.which(name)
        if found and Path(found).resolve().parent != (root / "bin").resolve():
            raise ReleaseError(
                "В PATH уже есть другой " + name + "; устраните конфликт, чужой файл не изменён."
            )


def launcher_files(root: Path) -> dict[Path, bytes]:
    # Стабильные launcher читают единственный active pointer при каждом запуске.
    script = """$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$file = Join-Path $root 'installation.json'
$gatePath = Join-Path $root '.launch-lock'
$gate = $null
$lease = $null
$leasePath = $null
$module = switch ($args[0]) {
    'review' { 'language_review' }
    'eval' { 'language_review.evaluation' }
    default { throw 'Неизвестный launcher Language Review.' }
}
$forward = @($args | Select-Object -Skip 1)
for ($attempt = 0; $attempt -lt 300; $attempt++) {
    try {
        $gate = [IO.File]::Open($gatePath, 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
        $gate.Lock(0, 1)
        break
    } catch [IO.IOException] {
        if ($gate) { $gate.Dispose(); $gate = $null }
        Start-Sleep -Milliseconds 100
    }
}
if (-not $gate) { throw 'Не удалось дождаться переключения active runtime.' }
try {
    $installation = Get-Content -Raw -LiteralPath $file | ConvertFrom-Json
    if ($installation.active -notmatch '^[a-f0-9]{32}$') { throw 'Некорректный active runtime.' }
    $release = Join-Path $root ('releases/' + $installation.active)
    $leaseDirectory = Join-Path $release '.run-leases'
    if (-not (Test-Path -LiteralPath (Join-Path $leaseDirectory 'protocol') -PathType Leaf)) {
        throw 'Runtime не поддерживает безопасное управление lease.'
    }
    $leasePath = Join-Path $leaseDirectory ([guid]::NewGuid().ToString('N') + '.lock')
    $lease = [IO.File]::Open($leasePath, 'CreateNew', 'ReadWrite', 'None')
    $lease.SetLength(1)
    $lease.Lock(0, 1)
    $lease.Write([Text.Encoding]::ASCII.GetBytes([string]$PID))
    $lease.Flush()
} catch {
    if ($lease) { $lease.Dispose() }
    if ($leasePath -and (Test-Path -LiteralPath $leasePath)) {
        Remove-Item -LiteralPath $leasePath -Force
    }
    throw
} finally {
    $gate.Dispose()
    Remove-Item -LiteralPath $gatePath -Force -ErrorAction SilentlyContinue
}
$python = Join-Path $release 'venv/Scripts/python.exe'
$exitCode = 1
$env:LANGUAGE_REVIEW_LAUNCHER_PID = [string]$PID
try {
    & $python -I -m $module @forward
    $exitCode = $LASTEXITCODE
} finally {
    $lease.Unlock(0, 1)
    $lease.Dispose()
    Remove-Item -LiteralPath $leasePath -Force
}
exit $exitCode
"""
    cmd = '@echo off\r\npwsh.exe -NoProfile -File "%~dp0language-review-launch.ps1" review %*\r\n'
    evaluator = (
        '@echo off\r\npwsh.exe -NoProfile -File "%~dp0language-review-launch.ps1" eval %*\r\n'
    )
    return {
        root / "bin/language-review.cmd": cmd.encode(),
        root / "bin/language-review-eval.cmd": evaluator.encode(),
        root / "bin/language-review-launch.ps1": script.encode("utf-8"),
    }


def install(
    bundle: Path,
    root: Path,
    *,
    uv: str,
    extras: str = "core",
    home: Path | None = None,
    agents: list[str] | None = None,
    add_path: bool | None = None,
    allow_downgrade: bool = False,
) -> dict[str, Any]:
    from language_review.release_contract import version_key

    if os.name != "nt":
        raise ReleaseError("Установщик поддерживает Windows.")
    if extras not in {"core", "chatgpt", "memory", "all"}:
        raise ReleaseError("Неизвестный набор extras.")
    root = root.absolute()
    home = home or Path.home()
    data = validate_bundle(bundle)
    with locked(root):
        old = state(root)
        if "active" not in old or old.get("uninstalled"):
            old = {}
        if add_path is None:
            add_path = old.get("path_requested", True)
        if old.get("version") == data["version"] and old.get("manifest_sha256") != digest(
            bundle / "release.json"
        ):
            raise ReleaseError(
                "Содержимое уже установленной версии изменилось; требуется новый tag/version."
            )
        path_conflict(root)
        launchers = launcher_files(root)
        for path in launchers:
            safe_path(path)
            if path.exists() and digest(path) != old.get("launchers", {}).get(path.name):
                raise ReleaseError(
                    "Launcher изменён пользователем или принадлежит другой установке."
                )
        if (
            old
            and version_key(data["version"]) < version_key(old["version"])
            and not allow_downgrade
        ):
            raise ReleaseError("Понижение версии требует --allow-downgrade; БД не откатывается.")
        selected = sorted(set(old.get("skills", {})) | set(agents or []))
        with zipfile.ZipFile(
            bundle / next(n for n in data["assets"] if n.endswith(".whl"))
        ) as wheel:
            content = wheel.read("language_review/distribution_assets/SKILL.md")
        writes, skills = skill_writes(old, selected, content, home)
        if (
            old.get("manifest_sha256") == digest(bundle / "release.json")
            and old.get("extras") == extras
        ):
            probe(root / "releases" / old["active"], data["package_version"])
            added = change_path(root / "bin", add=True) if add_path else False
            current = {
                **old,
                "skills": skills,
                "path_added": added or old.get("path_added", False),
                "path_requested": add_path,
            }
            writes.update(launchers)
            writes[root / "installation.json"] = json_bytes(current)
            try:
                transaction(root, writes)
            except BaseException:
                if added:
                    change_path(root / "bin", add=False)
                raise
            return current
        identifier = uuid.uuid4().hex
        release = root / "releases" / identifier
        release.mkdir(parents=True)
        added = False
        committed = False
        try:
            shutil.copytree(bundle, release / "bundle")
            (release / "tools").mkdir()
            private_uv = release / "tools/uv.exe"
            shutil.copyfile(uv, private_uv)
            lease_directory = release / ".run-leases"
            lease_directory.mkdir()
            (lease_directory / "protocol").write_text("1\n", encoding="ascii")
            execute([uv, "venv", "--no-config", "--python", "3.15", str(release / "venv")])
            python = str(python_at(release))
            from language_review.release_contract import unpack_wheelhouse

            unpack_wheelhouse(release / "bundle", release / "wheelhouse")
            execute(
                [
                    uv,
                    "pip",
                    "install",
                    "--no-config",
                    "--python",
                    python,
                    "--require-hashes",
                    "--no-index",
                    "--find-links",
                    str(release / "wheelhouse"),
                    "--only-binary",
                    ":all:",
                    "--compile-bytecode",
                    "-r",
                    str(release / "bundle" / f"requirements-{extras}.txt"),
                ]
            )
            wheel_path = next((release / "bundle").glob("*.whl"))
            execute(
                [
                    uv,
                    "pip",
                    "install",
                    "--no-config",
                    "--python",
                    python,
                    "--no-deps",
                    "--compile-bytecode",
                    str(wheel_path),
                ]
            )
            probe(release, data["package_version"])
            inventory = {
                str(p.relative_to(release)): digest(p)
                for p in release.rglob("*")
                if p.is_file() and not p.is_symlink()
            }
            atomic(release / "owned-files.json", json_bytes({"owner": OWNER, "files": inventory}))
            if add_path:
                added = change_path(root / "bin", add=True)
            current = {
                "owner": OWNER,
                "root": str(root),
                "active": identifier,
                "previous": old.get("active"),
                "version": data["version"],
                "package_version": data["package_version"],
                "commit": data["commit"],
                "manifest_sha256": digest(bundle / "release.json"),
                "extras": extras,
                "skills": skills,
                "home": str(home),
                "path_added": added or old.get("path_added", False),
                "path_requested": add_path,
                "uv": str(private_uv),
                "launchers": {
                    p.name: hashlib.sha256(content).hexdigest() for p, content in launchers.items()
                },
            }
            writes.update(launchers)
            writes[root / "installation.json"] = json_bytes(current)
            with launch_locked(root):
                transaction(root, writes)
                committed = True
        except BaseException:
            # Неизвестный результат восстановления не разрешает удалять поколение:
            # pointer или журнал всё ещё могут ссылаться на него.
            if not committed and not (root / ".transaction.json").exists():
                if state(root).get("active") == identifier:
                    raise
                if added:
                    change_path(root / "bin", add=False)
                # Только созданный этой операцией каталог; пользовательские данные вне него.
                if release.is_relative_to(root / "releases") and not release.is_symlink():
                    shutil.rmtree(release)
            raise
        with launch_locked(root), contextlib.suppress(OSError, ReleaseError):
            prune_stale_releases(root, active=identifier, previous=old.get("active"))
        return current


def managed_root() -> Path:
    for parent in Path(sys.prefix).parents:
        if (parent / "installation.json").is_file():
            return parent
    raise ReleaseError(
        "Команда требует управляемую установку; checkout не является установленным продуктом."
    )


def setup_skills(root: Path, agents: list[str]) -> None:
    with locked(root):
        old = state(root)
        if not old:
            raise ReleaseError("Управляемая установка не найдена.")
        bundle = root / "releases" / old["active"] / "bundle"
        validate_bundle(bundle)
        with zipfile.ZipFile(next(bundle.glob("*.whl"))) as wheel:
            content = wheel.read("language_review/distribution_assets/SKILL.md")
        writes, skills = skill_writes(old, agents, content, Path(old["home"]))
        writes[root / "installation.json"] = json_bytes({**old, "skills": skills})
        transaction(root, writes)


def rollback(root: Path) -> dict[str, Any]:
    old = state(root)
    previous = old.get("previous")
    if not previous or not isinstance(previous, str) or not re_identifier(previous):
        raise ReleaseError("Сохранённая предыдущая версия отсутствует.")
    bundle = root / "releases" / previous / "bundle"
    # install делает отдельный проверенный кандидат; необязательные worker/БД остаются у владельца.
    return install(
        bundle,
        root,
        uv=private_uv(root, old),
        extras=old["extras"],
        home=Path(old["home"]),
        allow_downgrade=True,
    )


def re_identifier(value: str) -> bool:
    import re

    return re.fullmatch(r"[a-f0-9]{32}", value) is not None


def status(root: Path) -> dict[str, Any]:
    old = state(root)
    if not old.get("active") or old.get("uninstalled"):
        return {"status": "not_installed"}
    skills = {}
    for agent, checksum in old["skills"].items():
        path = skill_path(agent, Path(old["home"]))
        skills[agent] = "current" if path.is_file() and digest(path) == checksum else "drift"
    launchers = {}
    for path in launcher_files(root):
        safe_path(path)
        try:
            launchers[path.name] = (
                "missing"
                if not path.is_file()
                else "current"
                if digest(path) == old.get("launchers", {}).get(path.name)
                else "changed"
            )
        except OSError:
            launchers[path.name] = "unreadable"
    runtime = "current" if python_at(root / "releases" / old["active"]).is_file() else "missing"
    return {
        "status": "issues"
        if (
            "drift" in skills.values()
            or runtime != "current"
            or any(value != "current" for value in launchers.values())
        )
        else "installed",
        "version": old["version"],
        "package_version": old["package_version"],
        "commit": old["commit"],
        "skills": skills,
        "launchers": launchers,
        "runtime": runtime,
        "extras": old["extras"],
        "previous_available": bool(old.get("previous")),
    }


def uninstall(root: Path) -> list[str]:
    preserved: list[str] = []
    with locked(root):
        old = state(root)
        if not old:
            raise ReleaseError("Управляемая установка отсутствует.")
        writes: dict[Path, bytes | None] = {}
        for agent, checksum in old["skills"].items():
            path = skill_path(agent, Path(old["home"]))
            safe_path(path)
            if path.exists() and digest(path) == checksum:
                writes[path] = None
            elif path.exists():
                preserved.append("Изменённый skill сохранён: " + agent)
        for path in launcher_files(root):
            if path.exists() and digest(path) == old.get("launchers", {}).get(path.name):
                writes[path] = None
            elif path.exists():
                preserved.append("Изменённый launcher сохранён: " + path.name)
        transaction(root, writes)
        if old["path_added"]:
            change_path(root / "bin", add=False)
        # Не удаляем текущий python.exe из собственного процесса: Windows держит его открытым.
        # Inventory остаётся для внешней очистки и обнаружения чужих изменений.
        atomic(root / "installation.json", json_bytes({**old, "uninstalled": True, "skills": {}}))
    return preserved


def schedule_cleanup(root: Path) -> None:
    if sys.platform != "win32":
        raise ReleaseError("Внешняя очистка поддерживается только в Windows.")
    from importlib.resources import files

    script = Path(tempfile.gettempdir()) / ("language-review-cleanup-" + uuid.uuid4().hex + ".ps1")
    script.write_bytes(
        files("language_review").joinpath("distribution_assets/uninstall-cleanup.ps1").read_bytes()
    )
    try:
        launcher_pid = int(os.environ.get("LANGUAGE_REVIEW_LAUNCHER_PID", "0"))
    except ValueError:
        launcher_pid = 0
    if not 0 < launcher_pid <= 0x7FFFFFFF:
        launcher_pid = 0
    subprocess.Popen(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(script),
            "-Root",
            str(root),
            "-WaitForPid",
            str(os.getpid()),
            "-WaitForParentPid",
            str(os.getppid()),
            "-WaitForLauncherPid",
            str(launcher_pid),
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--uv", required=True)
    parser.add_argument("--extras", default="core", choices=("core", "chatgpt", "memory", "all"))
    parser.add_argument("--agent", action="append", choices=("codex", "claude"))
    parser.add_argument("--no-path", action="store_true")
    args = parser.parse_args()
    try:
        result = install(
            args.bundle,
            args.root,
            uv=args.uv,
            extras=args.extras,
            agents=args.agent,
            add_path=not args.no_path,
        )
        print(
            json.dumps(
                {
                    "status": "core_installed",
                    "version": result["version"],
                    "next": "Откройте новый терминал; language-review setup",
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        print("Установка не завершена: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
