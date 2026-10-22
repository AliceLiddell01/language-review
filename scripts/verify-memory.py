"""Настоящая PostgreSQL/pgvector: restart, backup, потеря тома и восстановление."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from language_review.memory.database import Database


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run(
    arguments: list[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    scratch: Path,
    name: str,
    codes: tuple[int, ...] = (0,),
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        env=environment,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    password = environment.get("LANGUAGE_REVIEW_DB_PASSWORD", "")
    for suffix, content in (("stdout", result.stdout), ("stderr", result.stderr)):
        if password:
            content = content.replace(password, "[REDACTED]")
        (scratch / f"{name}.{suffix}.log").write_text(content, encoding="utf-8")
    if result.returncode not in codes:
        raise RuntimeError(f"Проверка {name}: код {result.returncode}; см. журналы приёмки.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-llm-model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--embedding-revision")
    parser.add_argument("--embedding-dimensions", type=int, default=768)
    arguments = parser.parse_args(argv)
    if arguments.embedding_model and not arguments.embedding_revision:
        parser.error("Для --embedding-model требуется --embedding-revision.")
    if os.name != "nt":
        parser.error("Приёмка выполняется в нативной среде Windows.")
    repo = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix="language-review-memory-"))
    project = "lr-memory-acceptance-" + uuid4().hex[:12]
    password = uuid4().hex
    port = available_port()
    environment = dict(os.environ)
    environment.update(
        {
            "LANGUAGE_REVIEW_DB_PASSWORD": password,
            "LANGUAGE_REVIEW_DB_PORT": str(port),
            "LANGUAGE_REVIEW_MEMORY_DSN": f"postgresql://language_review:{password}@127.0.0.1:{port}/language_review",
            "LANGUAGE_REVIEW_MEMORY_LIVE_LLM": arguments.live_llm_model or "",
            "LANGUAGE_REVIEW_MEMORY_EMBEDDING_MODEL": arguments.embedding_model or "",
            "LANGUAGE_REVIEW_MEMORY_EMBEDDING_REVISION": arguments.embedding_revision or "",
            "LANGUAGE_REVIEW_MEMORY_EMBEDDING_DIMENSIONS": str(arguments.embedding_dimensions),
            "APPDATA": str(scratch / "appdata"),
            "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
        }
    )
    environment["LANGUAGE_REVIEW_MEMORY_TEST_DSN"] = environment["LANGUAGE_REVIEW_MEMORY_DSN"]
    for name in ("APPDATA", "XDG_CONFIG_HOME"):
        Path(environment[name]).mkdir()
    database = Database(repo / "compose.memory.yml", project, environment=environment)
    summary: dict[str, Any] = {
        "status": "FAIL",
        "environment": "Windows/Python",
        "project": project,
        "live_llm": "NOT_RUN",
        "live_embeddings": "NOT_RUN",
        "cleanup_status": "NOT_RUN",
    }

    def command(
        args: list[str], name: str, codes: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        return run(args, environment=environment, cwd=repo, scratch=scratch, name=name, codes=codes)

    uv = ["uv", "run", "--locked", "--extra", "memory"]

    def cli(
        args: list[str], name: str, codes: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        return command([*uv, "language-review", *args], name, codes)

    def snapshot(name: str, *, expect: bool = True) -> None:
        command(
            [
                *uv,
                "python",
                "scripts/memory-snapshot.py",
                "--output",
                str(scratch / f"{name}.json"),
                *(["--expect", str(scratch / "before.json")] if expect else []),
            ],
            name,
        )

    try:
        database.up()
        cli(["memory", "migrate"], "migrate")
        cli(["memory", "health"], "health")
        command(
            [
                *uv,
                "pytest",
                "-n",
                "8",
                "tests/test_memory.py",
                "tests/test_memory_integration.py",
                "tests/test_memory_worker.py",
            ],
            "integration",
        )
        snapshot("before", expect=False)
        compose = ["docker", "compose", "-f", str(repo / "compose.memory.yml"), "-p", project]
        # Все операции адресуют только UUID project текущего запуска.
        command([*compose, "restart", "postgres"], "restart")
        database.up()
        snapshot("restarted")
        database.status()  # Повторная проверка ownership перед заменой контейнера.
        command([*compose, "up", "-d", "--force-recreate", "--wait"], "recreate")
        snapshot("recreated")
        summary["restart_recreate"] = "PASS"
        backup = scratch / "memory.dump"
        summary["backup"] = database.backup(backup)
        database.down(remove_volumes=True)
        database.up()
        database.restore(backup)
        cli(["memory", "health"], "restored-health")
        snapshot("restored")
        summary["database_loss_restore"] = "PASS"
        database.stop()
        source = scratch / "input.md"
        source.write_text("Корректный русский текст.", encoding="utf-8")
        cli(["init", str(scratch)], "init")
        failed = cli(
            [
                "check",
                str(source),
                "--engine",
                "vale",
                "--memory",
                "--memory-project",
                str(uuid4()),
                "--format",
                "json",
            ],
            "database-failure",
            (6,),
        )
        if failed.stdout:
            raise RuntimeError("Отказ БД должен оставлять stdout пустым.")
        cli(
            [
                "check",
                str(source),
                "--engine",
                "vale",
                "--no-memory",
                "--format",
                "json",
            ],
            "without-memory",
            (0, 1, 5),
        )
        summary.update(
            {
                "database_failure": "PASS",
                "status": "PASS",
                "live_llm": "PASS" if arguments.live_llm_model else "NOT_RUN",
                "live_embeddings": "PASS" if arguments.embedding_model else "NOT_RUN",
            }
        )
    except Exception as error:
        summary["failure"] = str(error).replace(password, "[REDACTED]")
    finally:
        try:
            if not re.fullmatch(r"lr-memory-acceptance-[0-9a-f]{12}", project):
                raise RuntimeError("Небезопасный project для очистки.")
            database.down(remove_volumes=True)
            summary["cleanup_status"] = "PASS"
        except Exception as error:
            summary.update(
                {"status": "FAIL", "cleanup_status": "FAIL", "cleanup_error": type(error).__name__}
            )
        (scratch / "acceptance.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Артефакты приёмки: {scratch}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
