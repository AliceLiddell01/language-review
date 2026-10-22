"""Запускает валидатор PR из trusted base с локальным fallback."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TRUSTED_BASE_WARNING = (
    "::warning::В базовом коммите ещё нет валидатора описания PR; "
    "используется валидатор из этого PR."
)


def choose_checker(repository_root: Path, trusted_base_root: Path) -> tuple[Path, bool]:
    trusted_checker = trusted_base_root / ".github" / "scripts" / "check_pr_body.py"
    if trusted_checker.is_file():
        return trusted_checker, True
    return repository_root / ".github" / "scripts" / "check_pr_body.py", False


def run_check(event_path: Path, repository_root: Path, trusted_base_root: Path) -> int:
    checker, trusted = choose_checker(repository_root, trusted_base_root)
    if not trusted:
        print(TRUSTED_BASE_WARNING)
    try:
        return subprocess.run(
            [sys.executable, str(checker), "--event-path", str(event_path)],
            cwd=repository_root,
            check=False,
        ).returncode
    except OSError as error:
        print(f"Не удалось запустить проверку описания PR: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Запустить проверку описания PR из trusted base.")
    parser.add_argument(
        "--event-path", type=Path, required=True, help="Путь к событию GitHub Actions"
    )
    parser.add_argument("--trusted-base", type=Path, default=Path("trusted-base"))
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    repository_root = args.repository_root.resolve()
    trusted_base_root = args.trusted_base
    if not trusted_base_root.is_absolute():
        trusted_base_root = repository_root / trusted_base_root
    return run_check(args.event_path, repository_root, trusted_base_root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
