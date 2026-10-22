"""Управление собственной PostgreSQL через Docker Compose без командной оболочки."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4


class DatabaseError(RuntimeError):
    """Безопасная диагностика: stderr Docker и учётные данные не публикуются."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{message} ({code}).")


class Database:
    def __init__(
        self,
        compose_file: Path,
        project: str,
        *,
        environment: Mapping[str, str] | None = None,
        docker: str = "docker",
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise DatabaseError("invalid_project", "Некорректная идентичность Compose project")
        self.compose_file = compose_file.resolve()
        self.project = project
        self.environment = dict(os.environ if environment is None else environment)
        self.docker = docker

    def _run(self, args: list[str], *, timeout: int = 180) -> str:
        try:
            result = subprocess.run(
                [self.docker, *args],
                env=self.environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DatabaseError("docker_unavailable", "Docker недоступен") from error
        if result.returncode:
            raise DatabaseError("docker_unavailable", "Команда Docker завершилась ошибкой")
        return result.stdout.strip()

    def _compose(self, args: list[str], *, timeout: int = 180) -> str:
        return self._run(
            ["compose", "-f", str(self.compose_file), "-p", self.project, *args], timeout=timeout
        )

    @staticmethod
    def _json(output: str) -> Any:
        try:
            return json.loads(output)
        except ValueError as error:
            raise DatabaseError(
                "docker_output_invalid", "Docker вернул некорректный JSON"
            ) from error

    def _guard(self) -> None:
        """Проверяем точные имена и labels до изменения контейнера или тома."""
        container = f"{self.project}-postgres-1"
        volume = f"{self.project}_memory-data"
        containers = self._run(
            [
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{container}$",
                "--format",
                "{{.Names}}",
            ]
        ).splitlines()
        if containers:
            data = self._json(self._run(["container", "inspect", container]))
            try:
                labels = data[0]["Config"]["Labels"]
                owned = (
                    containers == [container]
                    and labels["com.docker.compose.project"] == self.project
                    and labels["com.docker.compose.service"] == "postgres"
                )
            except KeyError, IndexError, TypeError:
                owned = False
            if not owned:
                raise DatabaseError("ownership_mismatch", "Контейнер не принадлежит этому project")
        volumes = self._run(
            ["volume", "ls", "--filter", f"name=^{volume}$", "--format", "{{.Name}}"]
        ).splitlines()
        if volumes:
            data = self._json(self._run(["volume", "inspect", volume]))
            try:
                labels = data[0]["Labels"]
                owned = (
                    volumes == [volume]
                    and labels["com.docker.compose.project"] == self.project
                    and labels["com.docker.compose.volume"] == "memory-data"
                )
            except KeyError, IndexError, TypeError:
                owned = False
            if not owned:
                raise DatabaseError("ownership_mismatch", "Том не принадлежит этому project")

    def status(self) -> list[dict[str, Any]]:
        self._guard()
        output = self._compose(["ps", "--all", "--format", "json", "postgres"])
        if not output:
            return []
        # Compose выпускается как с JSONL, так и с JSON-массивом.
        records = (
            self._json(output)
            if output.startswith("[")
            else [self._json(line) for line in output.splitlines() if line.strip()]
        )
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise DatabaseError("docker_output_invalid", "Docker вернул некорректный статус")
        return [dict(record) for record in records]

    def up(self, wait_timeout: int = 120) -> None:
        self._guard()
        self._compose(
            ["up", "-d", "--wait", "--wait-timeout", str(wait_timeout)],
            timeout=wait_timeout + 60,
        )

    def stop(self) -> None:
        self._guard()
        self._compose(["stop"])

    def down(self, *, remove_volumes: bool = False) -> None:
        self._guard()
        self._compose(["down", *(["--volumes"] if remove_volumes else [])])

    def _container(self) -> str:
        container = self._compose(["ps", "-q", "postgres"])
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise DatabaseError("database_unavailable", "Контейнер БД недоступен")
        return container

    @contextlib.contextmanager
    def _remote_file(self, prefix: str) -> Iterator[tuple[str, str]]:
        self._guard()
        container = self._container()
        remote = f"/tmp/lr-{prefix}-{uuid4().hex}.dump"
        try:
            yield container, remote
        finally:
            failure = sys.exception()
            try:
                self._compose(["exec", "-T", "postgres", "rm", "-f", remote])
            except DatabaseError as cleanup:
                if failure is None:
                    raise
                failure.add_note(str(cleanup))

    def backup(self, path: Path) -> dict[str, Any]:
        target = path.resolve()
        if target.exists():
            raise DatabaseError("backup_exists", "Файл backup уже существует")
        # cp пишет только во временный файл; hardlink публикует backup без перезаписи.
        handle, name = tempfile.mkstemp(prefix=".lr-backup-", dir=target.parent)
        os.close(handle)
        temporary = Path(name)
        try:
            with self._remote_file("backup") as (container, remote):
                self._compose(
                    [
                        "exec",
                        "-T",
                        "postgres",
                        "pg_dump",
                        "-U",
                        "language_review",
                        "-d",
                        "language_review",
                        "-Fc",
                        "-f",
                        remote,
                    ],
                    timeout=600,
                )
                self._run(["cp", f"{container}:{remote}", str(temporary)], timeout=600)
                self._compose(["exec", "-T", "postgres", "pg_restore", "--list", remote])
                with temporary.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                size = temporary.stat().st_size
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    raise
                except OSError:
                    if os.name != "nt":
                        raise
                    os.rename(temporary, target)
                return {"sha256": checksum, "size": size, "path": str(target)}
        finally:
            temporary.unlink(missing_ok=True)

    def restore(self, path: Path) -> None:
        source = path.resolve()
        if not source.is_file():
            raise DatabaseError("backup_missing", "Требуется существующий файл backup")
        self._guard()
        count = self._compose(
            [
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "language_review",
                "-d",
                "language_review",
                "-Atc",
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'",
            ]
        )
        if count != "0":
            raise DatabaseError("database_not_empty", "Восстановление требует пустой БД")
        with self._remote_file("restore") as (container, remote):
            self._run(["cp", str(source), f"{container}:{remote}"], timeout=600)
            self._compose(
                [
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    "language_review",
                    "-d",
                    "language_review",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    remote,
                ],
                timeout=600,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "status", "stop", "backup", "restore"])
    parser.add_argument("--project", default="language-review-memory")
    parser.add_argument("--compose-file", type=Path, default=Path("compose.memory.yml"))
    parser.add_argument("--backup-path", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.action in {"backup", "restore"} and arguments.backup_path is None:
        parser.error("Для backup/restore требуется --backup-path.")
    try:
        database = Database(arguments.compose_file, arguments.project)
        result: Any = {"status": "PASS", "action": arguments.action}
        if arguments.action == "status":
            result = database.status()
        elif arguments.action == "backup":
            result = database.backup(arguments.backup_path)
        elif arguments.action == "restore":
            database.restore(arguments.backup_path)
        elif arguments.action == "up":
            database.up()
        else:
            database.stop()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DatabaseError, OSError) as error:
        print(
            str(error) if isinstance(error, DatabaseError) else "Ошибка файлов backup.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
