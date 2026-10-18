#!/usr/bin/env python3
"""Проверяет, что Git не отслеживает локальные настройки и секреты."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

PROTECTED_IGNORE_PATHS = (
    ".env",
    ".env.private",
    "nested/.env",
    "nested/.env.private",
    "settings.local.toml",
    "nested/settings.local.toml",
    ".codex/local/private.toml",
)
ALLOWED_TRACKED_PATHS = (".env.example",)


class RussianArgumentParser(argparse.ArgumentParser):
    """Локализует стандартные заголовки справки argparse."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self.add_argument(
            "-h", "--help", action="help", help="Показать эту справку и завершить работу"
        )
        self._positionals.title = "Позиционные аргументы"
        self._optionals.title = "Параметры"

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "Использование:", 1)


def is_protected(path: str) -> bool:
    """Определяет защищённые имена локальных файлов по маскам репозитория."""
    normalized = PurePosixPath(path.replace("\\", "/"))
    name = normalized.name
    if name == ".env.example":
        return False
    if name == ".env" or name.startswith(".env.") or name.endswith(".local.toml"):
        return True
    return normalized.as_posix().startswith(".codex/local/")


def git_environment(global_config: Path) -> dict[str, str]:
    """Отключает пользовательские исключения Git для воспроизводимых проверок."""
    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        environment.pop(name, None)
    return environment


def ignored_by_project_rules(root: Path, paths: tuple[str, ...]) -> tuple[set[str], list[str]]:
    """Проверяет фактическое поведение .gitignore командой git check-ignore."""
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return set(), ["Отсутствует .gitignore"]

    ignored: set[str] = set()
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        sandbox = Path(temporary)
        config = sandbox / "empty.gitconfig"
        config.write_text("", encoding="utf-8")
        (sandbox / ".gitignore").write_bytes(ignore_file.read_bytes())
        init = subprocess.run(
            ["git", "-C", str(sandbox), "init", "--quiet"],
            capture_output=True,
            env=git_environment(config),
            check=False,
        )
        if init.returncode:
            return set(), ["Не удалось создать временный Git-репозиторий для проверки .gitignore"]

        for path in paths:
            check = subprocess.run(
                ["git", "-C", str(sandbox), "check-ignore", "--no-index", "--quiet", "--", path],
                capture_output=True,
                env=git_environment(config),
                check=False,
            )
            if check.returncode == 0:
                ignored.add(path)
            elif check.returncode != 1:
                errors.append(f"git check-ignore завершился с кодом {check.returncode}")
                break
    return ignored, errors


def tracked_protected_paths(root: Path) -> tuple[list[str], list[str]]:
    """Возвращает только пути защищённых файлов из текущего Git-индекса."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return [], ["Не удалось прочитать Git-индекс"]
    paths = (
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )
    return sorted(path for path in paths if is_protected(path)), []


def validate_local_data(root: Path) -> list[str]:
    """Проверяет защищённые ignore-маски и файлы в Git-индексе."""
    root = root.resolve()
    try:
        ignore_file = root / ".gitignore"
        if not ignore_file.is_file():
            return ["Отсутствует .gitignore"]
        ignore_lines = ignore_file.read_text(encoding="utf-8").splitlines()
        errors = [
            f"Неподтверждённое исключение ослабляет защиту .gitignore: {line.strip()}"
            for line in ignore_lines
            if line.strip().startswith("!") and line.strip() != "!.env.example"
        ]
        ignore_paths = set(PROTECTED_IGNORE_PATHS)
        ignored, ignore_errors = ignored_by_project_rules(root, tuple(sorted(ignore_paths)))
        errors.extend(ignore_errors)
        errors.extend(
            f".gitignore не защищает обязательный путь: {path}"
            for path in sorted(ignore_paths - ignored)
        )
        allowed_ignored, allowed_errors = ignored_by_project_rules(root, ALLOWED_TRACKED_PATHS)
        errors.extend(allowed_errors)
        errors.extend(
            f".gitignore не должен скрывать разрешённый файл: {path}"
            for path in sorted(allowed_ignored)
        )
        tracked, tracked_errors = tracked_protected_paths(root)
        errors.extend(tracked_errors)
    except OSError:
        return ["Не удалось проверить правила локальных данных"]
    errors.extend(
        f"Защищённый локальный путь уже находится в Git-индексе: {path}" for path in tracked
    )
    return errors


def main() -> int:
    """Проверяет индекс Git и правила .gitignore для локальных данных."""
    parser = RussianArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Корень репозитория")
    args = parser.parse_args()
    errors = validate_local_data(args.root)
    if errors:
        for error in errors:
            print(f"::error::{error}", file=sys.stderr)
        return 1
    print("Локальные настройки и секреты защищены от попадания в Git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
