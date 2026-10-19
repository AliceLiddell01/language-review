"""Явный live-запуск публичного check; отчёты и исходники разнесены по каталогам."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

from language_review.errors import InputError
from language_review.evaluation.comparison import distribution
from language_review.evaluation.contracts import digest, save_new
from language_review.evaluation.corpus import safe_path, source_map
from language_review.evaluation.reports import load_product_report
from language_review.evaluation.scoring import score


def materialize(corpus: dict[str, Any], split: str, destination: Path) -> dict[str, str]:
    """Экспорт только исходников выбранного split, без меток/ответов test."""
    destination.mkdir(parents=True, exist_ok=False)
    selected = {case["document"] for case in corpus["cases"] if case["split"] == split}
    documents = [doc for doc in corpus["documents"] if doc["id"] in selected]
    repositories = sorted({doc["repository"] for doc in documents})
    for document in documents:
        target = destination / safe_path(document["repository"]) / safe_path(document["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(source_map(document).raw)
    return {f"root-{index:04d}": repository for index, repository in enumerate(repositories, 1)}


def capture(
    corpus: dict[str, Any],
    split: str,
    engines: list[str],
    config: Path | None,
    destination: Path,
    product_commit: str,
    repetitions: int = 1,
    timeout: float = 600,
) -> dict[str, Any]:
    if not 1 <= repetitions <= 100 or timeout <= 0:
        raise InputError("Нужно 1–100 повторений и положительный timeout.")
    destination.mkdir(parents=True, exist_ok=False)
    results = []
    with tempfile.TemporaryDirectory(prefix="language-review-eval-") as temporary:
        inputs = Path(temporary) / "inputs"
        bindings = materialize(corpus, split, inputs)
        if not bindings:
            raise InputError("В выбранном split нет документов.")
        initial = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in inputs.rglob("*")
            if path.is_file()
        }
        command = [sys.executable, "-m", "language_review"]
        if config:
            command += ["--config", str(config.resolve(strict=True))]
        command += [
            "check",
            *(str(inputs / repo) for repo in bindings.values()),
            "--format",
            "json",
            "--view",
            "review",
        ]
        for engine in engines:
            command += ["--engine", engine]
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        for repetition in range(1, repetitions + 1):
            start = perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=environment,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                save_new(
                    destination / f"not-run-{repetition}.json",
                    {
                        "status": "NOT_RUN",
                        "reason": "public_check_timeout",
                        "repetition": repetition,
                    },
                )
                continue
            elapsed = (perf_counter() - start) * 1000
            if any(
                hashlib.sha256(path.read_bytes()).hexdigest() != expected
                for path, expected in initial.items()
            ):
                raise InputError("Публичный check изменил материализованный исходник.")
            if completed.returncode not in {0, 1, 4, 5} or not completed.stdout.strip():
                save_new(
                    destination / f"not-run-{repetition}.json",
                    {
                        "status": "NOT_RUN",
                        "reason": "public_check_no_valid_report",
                        "exit_code": completed.returncode,
                        "repetition": repetition,
                    },
                )
                continue
            report_path = destination / f"report-{repetition}.json"
            # CLI stdout сохраняется как есть; stderr с частными путями не публикуется.
            with report_path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(completed.stdout)
            try:
                report = load_product_report(report_path)
                manifest = {
                    "schema": "language-review.eval-run",
                    "schema_version": "1.0",
                    "corpus_sha256": digest(corpus),
                    "split": split,
                    "report_sha256": digest(report),
                    "root_bindings": bindings,
                    "product_commit": product_commit,
                    "configuration_sha256": hashlib.sha256(config.read_bytes()).hexdigest()
                    if config
                    else None,
                    "wall_clock_ms": elapsed,
                    "exit_code": completed.returncode,
                    "capture": "live_public_cli",
                    "parameters": {"engines": engines, "view": "review"},
                    "repetition": repetition,
                }
                result = score(corpus, report, bindings, split, manifest)
            except InputError:
                save_new(
                    destination / f"not-run-{repetition}.json",
                    {
                        "status": "NOT_RUN",
                        "reason": "public_check_invalid_report",
                        "exit_code": completed.returncode,
                        "repetition": repetition,
                    },
                )
                continue
            save_new(destination / f"run-{repetition}.json", manifest)
            save_new(destination / f"evaluation-{repetition}.json", result)
            results.append(result)
    aggregate = distribution(results)
    status = (
        "PASS"
        if len(results) == repetitions and all(result["status"] == "PASS" for result in results)
        else "PARTIAL"
        if results
        else "NOT_RUN"
    )
    aggregate.update({"status": status, "requested_repetitions": repetitions})
    save_new(destination / "distribution.json", aggregate)
    return aggregate
