"""Воспроизводимый capture pilot оценки reference-корпусов."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ("vale", "languagetool", "llm")
SOURCES = (
    ("rust-book-ru", "dev", "book_limit"),
    ("sage-github-ru", "test", "pair_limit"),
    ("cubic-spline-ru", "validation", "code_limit"),
    ("xnet-ru", "validation", "code_limit"),
    ("far-ru", "validation", "code_limit"),
    ("tglock-cli-ru", "validation", "code_limit"),
)


class PilotError(RuntimeError):
    """Ошибка подготовки входов или выполнения reference pilot."""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--destination", type=Path, required=True)
    result.add_argument("--config", type=Path)
    result.add_argument("--engine", action="append", choices=ENGINES, default=None)
    result.add_argument("--cache-root", type=Path)
    for name, default in (("pair-limit", 24), ("book-limit", 250), ("code-limit", 24)):
        result.add_argument(
            "--" + name,
            type=bounded_limit,
            default=default,
        )
    return result


def bounded_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 10000:
        raise argparse.ArgumentTypeError("значение должно быть от 1 до 10000")
    return parsed


def resolve_existing(path: Path, label: str) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise PilotError(f"{label} должен указывать на существующий путь.") from exc


def prepare_destination(value: Path, repository: Path) -> Path:
    try:
        parent = value.parent.resolve(strict=True)
    except OSError as exc:
        raise PilotError("Родительский каталог Destination должен существовать.") from exc
    if not parent.is_dir():
        raise PilotError("Родительский каталог Destination должен существовать.")

    target = parent / value.name
    root = repository.resolve()
    if is_inside_repository(target, root):
        raise PilotError("Данные и результаты должны находиться вне checkout.")
    if target.exists() or target.is_symlink():
        raise PilotError("Нужен новый каталог артефактов.")
    return target


def is_inside_repository(path: Path, repository: Path) -> bool:
    path_text = os.path.normcase(os.path.abspath(path))
    root_text = os.path.normcase(os.path.abspath(repository.resolve()))
    try:
        return os.path.commonpath((path_text, root_text)) == root_text
    except ValueError:
        return False


def command(
    arguments: Sequence[str],
    *,
    cwd: Path,
    stdout: BinaryIO | int | None = None,
    allowed: set[int] | None = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, cwd=cwd, stdout=stdout, check=False)
    except OSError as exc:
        raise PilotError("Не удалось выполнить обязательную команду.") from exc
    accepted = allowed if allowed is not None else {0}
    if result.returncode not in accepted:
        raise PilotError(f"Команда завершилась с кодом {result.returncode}.")
    return result


def command_text(arguments: Sequence[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise PilotError("Не удалось выполнить обязательную команду.") from exc
    if result.returncode:
        raise PilotError(f"Команда завершилась с кодом {result.returncode}.")
    return result.stdout.strip()


def evaluation(
    repository: Path,
    arguments: Sequence[str],
    *,
    output: Path | None = None,
    allowed: set[int] | None = None,
) -> None:
    stream: BinaryIO | None = None
    try:
        if output is not None:
            stream = output.open("xb")
        command(
            ["uv", "run", "--locked", "language-review-eval", *arguments],
            cwd=repository,
            stdout=stream,
            allowed=allowed,
        )
    finally:
        if stream is not None:
            stream.close()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise PilotError("Не удалось прочитать JSON-артефакт оценки.") from exc
    if not isinstance(value, dict):
        raise PilotError("Ожидался JSON-объект оценки.")
    return value


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def require_equal_files(first: Path, second: Path, message: str) -> None:
    if sha256(first) != sha256(second):
        raise PilotError(message)


def git_state(repository: Path) -> tuple[str, str]:
    sha = command_text(["git", "rev-parse", "HEAD"], cwd=repository)
    status = command_text(["git", "status", "--porcelain=v1"], cwd=repository)
    return sha, status


def run_pilot(arguments: argparse.Namespace, repository: Path = ROOT) -> Path:
    repository = repository.resolve()
    target = prepare_destination(arguments.destination, repository)
    config = resolve_existing(arguments.config, "Config") if arguments.config else None
    cache_root = (
        resolve_existing(arguments.cache_root, "CacheRoot") if arguments.cache_root else None
    )
    if cache_root and is_inside_repository(cache_root, repository):
        raise PilotError("CacheRoot должен находиться вне checkout.")
    engines = arguments.engine or ["vale"]

    sha, status = git_state(repository)
    if status:
        raise PilotError("Нужен чистый committed checkout для привязки capture к SHA.")
    command(["uv", "sync", "--locked"], cwd=repository)
    target.mkdir()
    metadata = {
        "product_commit": sha,
        "python": command_text(["uv", "run", "--locked", "python", "--version"], cwd=repository),
        "uv": command_text(["uv", "--version"], cwd=repository),
        "automation_python": platform.python_version(),
        "engines": engines,
        "config_sha256": sha256(config).hex() if config else None,
        "protocol": (
            "Совпадение с опубликованной правкой; черновая разметка; без независимого baseline"
        ),
    }
    write_json(target / "environment.json", metadata)

    corpora = []
    for name, split, limit_key in SOURCES:
        bundle = target / name
        limit = getattr(arguments, limit_key)
        import_arguments = ["import-source", name, "--limit", str(limit), "--output", str(bundle)]
        if cache_root:
            import_arguments.extend(("--cache", str(cache_root / name)))
        evaluation(
            repository,
            import_arguments,
            output=target / f"{name}-import.json",
        )

        corpus_path = bundle / "corpus.json"
        corpora.append(read_json(corpus_path))
        evaluation(
            repository,
            ["validate", str(corpus_path)],
            output=target / f"{name}-validation.json",
        )
        blind = target / f"{name}-blind"
        evaluation(
            repository,
            ["annotation-export", str(corpus_path), "--split", split, "--output", str(blind)],
        )
        untouched = target / f"{name}-untouched.json"
        evaluation(
            repository,
            ["annotation-import", str(corpus_path), str(blind), "--output", str(untouched)],
        )
        if compact_json(read_json(corpus_path)) != compact_json(read_json(untouched)):
            raise PilotError("Незаполненный пакет неожиданно изменил разметку.")

        if split == "test":
            frozen = target / f"{name}-frozen.json"
            evaluation(
                repository,
                ["freeze-test", str(corpus_path), "--output", str(frozen)],
            )

    combined = dict(corpora[0])
    combined["id"] = "real-reference-pilot"
    combined["version"] = sha
    combined["documents"] = [item for corpus in corpora for item in corpus["documents"]]
    combined["cases"] = [item for corpus in corpora for item in corpus["cases"]]
    combined["split_policy"] = dict(combined["split_policy"])
    combined["split_policy"]["strategy"] = (
        "Разные именованные семейства репозиториев; проверка нормализованных и близких "
        "дублей между всеми splits; исходные репозитории SAGE неизвестны"
    )
    combined_path = target / "combined-corpus.json"
    write_json(combined_path, combined)
    evaluation(
        repository,
        ["validate", str(combined_path)],
        output=target / "cross-split-validation.json",
    )

    for name, split, _limit_key in SOURCES:
        bundle = target / name
        corpus_path = target / f"{name}-frozen.json" if split == "test" else bundle / "corpus.json"
        run_path = target / f"{name}-run"
        run_arguments = [
            "run",
            str(corpus_path),
            "--split",
            split,
            "--product-commit",
            sha,
            "--output",
            str(run_path),
            "--timeout",
            "900",
        ]
        for engine in engines:
            run_arguments.extend(("--engine", engine))
        if config:
            run_arguments.extend(("--config", str(config)))
        evaluation(
            repository,
            run_arguments,
            output=target / f"{name}-capture.json",
            allowed={4},
        )

        report = run_path / "report-1.json"
        manifest = run_path / "run-1.json"
        if not report.is_file():
            raise PilotError(f"Нет действительного live report: {name}")
        rescored = target / f"{name}-rescored.json"
        evaluation(
            repository,
            [
                "score",
                str(corpus_path),
                str(report),
                "--split",
                split,
                "--run-manifest",
                str(manifest),
                "--format",
                "json",
                "--output",
                str(rescored),
            ],
            allowed={4},
        )
        evaluation(
            repository,
            [
                "compare",
                str(run_path / "evaluation-1.json"),
                str(rescored),
                "--format",
                "json",
                "--output",
                str(target / f"{name}-compare.json"),
            ],
        )
        require_equal_files(
            run_path / "evaluation-1.json",
            rescored,
            "Повторный offline scoring не совпал побайтово.",
        )

        if name == "sage-github-ru":
            reference_outputs = []
            for replay in (1, 2):
                output = target / f"reference-{replay}.json"
                evaluation(
                    repository,
                    [
                        "reference-score",
                        str(corpus_path),
                        str(report),
                        "--source-bundle",
                        str(bundle),
                        "--run-manifest",
                        str(manifest),
                        "--split",
                        "test",
                        "--output",
                        str(output),
                    ],
                    allowed={4},
                )
                reference_outputs.append(output)
            require_equal_files(
                *reference_outputs,
                "Повторный reference assessment не совпал побайтово.",
            )

    final_sha, final_status = git_state(repository)
    if final_sha != sha or final_status:
        raise PilotError("Source checkout изменился при capture.")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        target = run_pilot(arguments)
    except PilotError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Ошибка доступа к файлу или записи артефакта.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(f"Артефакты: {target}. Независимая разметка и judgments не выполнялись.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
