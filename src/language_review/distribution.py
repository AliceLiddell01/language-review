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
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
            recover(root)
            yield
    finally:
        lock.unlink(missing_ok=True)


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
    except BaseException:
        recover(root)
        raise
    (root / ".transaction.json").unlink()


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
    # Недоступные optional движки — допустимый диагностический результат ядра.
    report = json.loads(
        execute(
            [python, "-I", "-m", "language_review", "doctor", "--format", "json"], expected={0, 3}
        )
    )
    if report["package"]["version"] != version or report["configuration"]["status"] != "valid":
        raise ReleaseError("Диагностика кандидата не соответствует поставке.")


def change_path(bin_path: Path, *, add: bool) -> bool:
    if os.name != "nt":
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
$installation = Get-Content -Raw -LiteralPath $file | ConvertFrom-Json
$python = Join-Path $root ('releases/' + $installation.active + '/venv/Scripts/python.exe')
$module = switch ($args[0]) {
    'review' { 'language_review' }
    'eval' { 'language_review.evaluation' }
    default { throw 'Неизвестный launcher Language Review.' }
}
$forward = @($args | Select-Object -Skip 1)
& $python -I -m $module @forward
exit $LASTEXITCODE
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
        selected = list(old.get("skills", {})) if agents is None else agents
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
            current = {**old, "skills": skills}
            writes.update(launchers)
            writes[root / "installation.json"] = json_bytes(current)
            transaction(root, writes)
            return current
        identifier = uuid.uuid4().hex
        release = root / "releases" / identifier
        release.mkdir(parents=True)
        added = False
        try:
            shutil.copytree(bundle, release / "bundle")
            (release / "tools").mkdir()
            private_uv = release / "tools/uv.exe"
            shutil.copyfile(shutil.which(uv) or uv, private_uv)
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
            transaction(root, writes)
            return current
        except BaseException:
            if added:
                change_path(root / "bin", add=False)
            # Только созданный этой операцией каталог; пользовательские данные вне него.
            if release.is_relative_to(root / "releases") and not release.is_symlink():
                shutil.rmtree(release)
            raise


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
    # install делает отдельный проверенный кандидат; optional worker/DB остаются у владельца.
    return install(
        bundle,
        root,
        uv=old["uv"],
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
    return {
        "status": "issues" if "drift" in skills.values() else "installed",
        "version": old["version"],
        "package_version": old["package_version"],
        "commit": old["commit"],
        "skills": skills,
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
    from importlib.resources import files

    script = Path(tempfile.gettempdir()) / ("language-review-cleanup-" + uuid.uuid4().hex + ".ps1")
    script.write_bytes(
        files("language_review").joinpath("distribution_assets/uninstall-cleanup.ps1").read_bytes()
    )
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
