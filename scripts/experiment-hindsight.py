"""Изолированный диагностический запуск Hindsight с локальными моделями."""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

IMAGE = (
    "ghcr.io/vectorize-io/hindsight:1.0.0"
    "@sha256:a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c"
)
ROOT = Path(__file__).resolve().parents[1]


class ExperimentError(RuntimeError):
    """Ошибка подготовки или выполнения изолированного эксперимента."""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, required=True)
    return result


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def api_description(base_url: str, *, attempts: int = 60, interval: float = 2) -> dict:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(base_url + "/openapi.json", timeout=2) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ExperimentError("Некорректный ответ OpenAPI Hindsight.")
            return value
        except OSError, urllib.error.URLError, json.JSONDecodeError:
            time.sleep(interval)
    raise ExperimentError("Hindsight не перешёл в состояние готовности.")


def container_id(cid_file: Path) -> str | None:
    try:
        value = cid_file.read_text("ascii").strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def docker_run(name: str, port: int, cid_file: Path) -> list[str]:
    return [
        "docker",
        "run",
        "-d",
        "--cidfile",
        str(cid_file),
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:8888",
        "-e",
        "HINDSIGHT_API_LLM_PROVIDER=ollama",
        "-e",
        "HINDSIGHT_API_LLM_MODEL=qwen3.5:4b",
        "-e",
        "HINDSIGHT_API_LLM_BASE_URL=http://host.docker.internal:11434/v1",
        "-e",
        "HINDSIGHT_API_LLM_OLLAMA_NUM_CTX=16384",
        "-e",
        "HINDSIGHT_API_LLM_STRICT_SCHEMA=true",
        "-e",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai",
        "-e",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=http://host.docker.internal:11434/v1",
        "-e",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=embeddinggemma:300m",
        "-e",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=local",
        "-e",
        "HINDSIGHT_API_RERANKER_PROVIDER=rrf",
        IMAGE,
    ]


def run_experiment(output: Path) -> int:
    if output.exists() or output.is_symlink():
        raise ExperimentError("Файл Output уже существует.")

    name = "lr-hindsight-experiment-" + uuid4().hex
    port = reserve_port()
    benchmark = Path(__file__).with_name("benchmark-memory.py").resolve()
    container_cleanup_failed = False
    benchmark_exit_code = 0
    with tempfile.TemporaryDirectory(prefix="lr-hindsight-experiment-") as temporary:
        cid_file = Path(temporary) / "container.id"
        try:
            started = subprocess.run(docker_run(name, port, cid_file), check=False)
            if started.returncode:
                raise ExperimentError("Не удалось запустить изолированный эксперимент Hindsight.")

            base_url = f"http://127.0.0.1:{port}"
            api = api_description(base_url)
            info = api.get("info")
            version = info.get("version") if isinstance(info, dict) else None
            if version != "1.0.0":
                raise ExperimentError(f"Неожиданная версия API: {version}")

            measured = subprocess.run(
                [
                    "uv",
                    "run",
                    "--project",
                    str(ROOT),
                    "--locked",
                    "--extra",
                    "memory",
                    "python",
                    str(benchmark),
                    "--hindsight",
                    base_url,
                    "--output",
                    str(output),
                ],
                check=False,
            )
            if measured.returncode:
                print("Benchmark Hindsight не выполнен.", file=sys.stderr)
            benchmark_exit_code = measured.returncode
        finally:
            current_id = container_id(cid_file)
            if current_id is not None:
                removed = subprocess.run(["docker", "rm", "--force", current_id], check=False)
                container_cleanup_failed = removed.returncode != 0
                if container_cleanup_failed:
                    print("Не удалось удалить контейнер текущего эксперимента.", file=sys.stderr)

    if container_cleanup_failed and benchmark_exit_code == 0:
        return 1
    if benchmark_exit_code == 0:
        print(f"Артефакты: {output}. Независимая разметка и judgments не выполнялись.")
    return benchmark_exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        return run_experiment(arguments.output)
    except ExperimentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Не удалось выполнить команду или записать артефакт.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
