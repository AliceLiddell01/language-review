"""Запускает валидатор PR из trusted base с локальным fallback."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def choose_checker(repository_root: Path, trusted_base_root: Path) -> tuple[Path, bool]:
    trusted_checker = trusted_base_root / ".github" / "scripts" / "check_pr_body.py"
    if trusted_checker.is_file():
        return trusted_checker, True
    return repository_root / ".github" / "scripts" / "check_pr_body.py", False


def run_check(event_path: Path, repository_root: Path, trusted_base_root: Path) -> int:
    checker, _ = choose_checker(repository_root, trusted_base_root)
    try:
        spec = importlib.util.spec_from_file_location("trusted_pr_body_checker", checker)
        if spec is None or spec.loader is None:
            raise ImportError
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        payload = json.loads(event_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("pull_request"), dict):
            print("Не удалось проверить событие: отсутствует pull_request.", file=sys.stderr)
            return 2
        if validator.is_automation_pr(payload):
            print("PR dependency-бота: формат ручных PR не применяется")
            return 0
        body = payload["pull_request"].get("body") or ""
        problems = validator.validate(body if isinstance(body, str) else "")
    except Exception:
        print("Не удалось загрузить валидатор или проверить событие PR.", file=sys.stderr)
        return 2
    if problems:
        print("::error::Описание PR не соответствует обязательной структуре.", file=sys.stderr)
        return 1
    print("Структура описания PR соответствует контракту")
    return 0


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
