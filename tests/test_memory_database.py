"""Контракты Docker lifecycle и бинарного backup без настоящих пользовательских томов."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from language_review.memory import database as database_module
from language_review.memory.database import Database, DatabaseError, main


@pytest.fixture
def docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Database, list[list[str]], dict[str, Any]]:
    calls: list[list[str]] = []
    replies: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "shell" not in kwargs
        assert kwargs["env"] == {"LANGUAGE_REVIEW_DB_PASSWORD": "private-test-secret"}
        assert kwargs["stdin"] == subprocess.DEVNULL
        calls.append(command)
        args = command[1:]
        output = ""
        if args[:2] == ["container", "ls"]:
            output = replies.get("container", "")
        elif args[:2] == ["volume", "ls"]:
            output = replies.get("volume", "")
        elif args[:2] == ["container", "inspect"]:
            output = json.dumps([{"Config": {"Labels": replies["container_labels"]}}])
        elif args[:2] == ["volume", "inspect"]:
            output = json.dumps([{"Labels": replies["volume_labels"]}])
        elif args[0] == "cp":
            if args[1].startswith("a" * 64 + ":"):
                Path(args[2]).write_bytes(b"PGDMP\x00\xff\r\n\x80")
        elif "ps" in args:
            output = replies.get("status", "") if "--format" in args else "a" * 64
        elif "psql" in args:
            output = replies.get("tables", "0")
        if replies.get("fail") and replies["fail"] in args:
            return subprocess.CompletedProcess(command, 8, output, "private-test-secret")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    return (
        Database(
            tmp_path / "compose.yml",
            "lr-test",
            environment={"LANGUAGE_REVIEW_DB_PASSWORD": "private-test-secret"},
        ),
        calls,
        replies,
    )


@pytest.mark.parametrize(
    "project",
    ["project with spaces", "../other", "-p", "Project", ""],
    ids=["spaces", "traversal", "option", "uppercase", "empty"],
)
def test_invalid_project_fails_before_docker(
    project: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Docker вызван"))
    with pytest.raises(DatabaseError, match="invalid_project"):
        Database(tmp_path / "compose.yml", project)


@pytest.mark.parametrize("resource", ["container", "volume"])
def test_up_refuses_foreign_exact_resource_labels(
    docker: tuple[Database, list[list[str]], dict[str, Any]], resource: str
) -> None:
    database, calls, replies = docker
    replies[resource] = "lr-test-postgres-1" if resource == "container" else "lr-test_memory-data"
    replies[resource + "_labels"] = {"com.docker.compose.project": "foreign"}
    with pytest.raises(DatabaseError, match="ownership_mismatch"):
        database.up()
    assert not any("up" in call for call in calls)


def test_up_stop_and_down_keep_project_scope_and_volume_default(
    docker: tuple[Database, list[list[str]], dict[str, Any]],
) -> None:
    database, calls, _ = docker
    database.up(30)
    database.stop()
    database.down()
    compose = [call for call in calls if "compose" in call]
    assert [call[6:] for call in compose] == [
        ["up", "-d", "--wait", "--wait-timeout", "30"],
        ["stop"],
        ["down"],
    ]
    assert all(call[4:6] == ["-p", "lr-test"] for call in compose)
    database.down(remove_volumes=True)
    assert calls[-1][-2:] == ["down", "--volumes"]


@pytest.mark.parametrize(
    "output",
    ['{"Service":"postgres","State":"running"}\n', '[{"Service":"postgres","State":"running"}]'],
    ids=["jsonl", "array"],
)
def test_status_accepts_compose_json_formats(
    docker: tuple[Database, list[list[str]], dict[str, Any]], output: str
) -> None:
    database, _, replies = docker
    replies["status"] = output
    assert database.status() == [{"Service": "postgres", "State": "running"}]


@pytest.mark.parametrize(
    "output", ["invalid", "[1]", '"string"'], ids=["not-json", "non-object-array", "scalar"]
)
def test_status_rejects_invalid_compose_output(
    docker: tuple[Database, list[list[str]], dict[str, Any]], output: str
) -> None:
    database, _, replies = docker
    replies["status"] = output
    with pytest.raises(DatabaseError, match="docker_output_invalid"):
        database.status()


def test_backup_copies_binary_checks_archive_and_hashes_before_publish(
    docker: tuple[Database, list[list[str]], dict[str, Any]], tmp_path: Path
) -> None:
    database, calls, _ = docker
    target = tmp_path / "backup with spaces.dump"
    result = database.backup(target)
    expected = b"PGDMP\x00\xff\r\n\x80"
    assert target.read_bytes() == expected
    assert result == {
        "path": str(target.resolve()),
        "size": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }
    assert any("-Fc" in call for call in calls)
    assert any("pg_restore" in call and "--list" in call for call in calls)
    assert calls[-1][-3:-1] == ["rm", "-f"]
    assert not list(tmp_path.glob(".lr-backup-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows может запрещать создание hard link")
def test_backup_falls_back_to_atomic_rename_when_hardlinks_are_unavailable(
    docker: tuple[Database, list[list[str]], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _ = docker

    def reject_hardlink(_source: Path, _target: Path) -> None:
        raise OSError("hard links are unavailable")

    monkeypatch.setattr(database_module.os, "link", reject_hardlink)
    target = tmp_path / "backup.dump"

    result = database.backup(target)

    assert target.read_bytes() == b"PGDMP\x00\xff\r\n\x80"
    assert result["path"] == str(target.resolve())
    assert not list(tmp_path.glob(".lr-backup-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows rename must preserve an existing target")
def test_backup_never_renames_over_target_created_during_publication(
    docker: tuple[Database, list[list[str]], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _ = docker
    target = tmp_path / "racing-backup.dump"

    def publish_competing_file(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"concurrent")
        raise FileExistsError(destination)

    monkeypatch.setattr(database_module.os, "link", publish_competing_file)
    monkeypatch.setattr(
        database_module.os,
        "rename",
        lambda *_args, **_kwargs: pytest.fail("Нельзя заменять существующий backup"),
    )

    with pytest.raises(FileExistsError):
        database.backup(target)

    assert target.read_bytes() == b"concurrent"
    assert not list(tmp_path.glob(".lr-backup-*"))


def test_backup_never_overwrites_existing_file(
    docker: tuple[Database, list[list[str]], dict[str, Any]], tmp_path: Path
) -> None:
    database, calls, _ = docker
    target = tmp_path / "existing.dump"
    target.write_bytes(b"original")
    with pytest.raises(DatabaseError, match="backup_exists"):
        database.backup(target)
    assert target.read_bytes() == b"original"
    assert calls == []


def test_failed_backup_checks_leave_no_final_or_temporary_file(
    docker: tuple[Database, list[list[str]], dict[str, Any]], tmp_path: Path
) -> None:
    database, calls, replies = docker
    replies["fail"] = "pg_restore"
    target = tmp_path / "failed.dump"
    with pytest.raises(DatabaseError, match="docker_unavailable") as captured:
        database.backup(target)
    assert "private-test-secret" not in str(captured.value)
    assert not target.exists()
    assert not list(tmp_path.glob(".lr-backup-*"))
    assert calls[-1][-3:-1] == ["rm", "-f"]


@pytest.mark.parametrize(
    "tables", ["1", "42", "invalid", ""], ids=["one", "many", "invalid", "empty-output"]
)
def test_restore_refuses_nonempty_or_unproven_database_before_copy(
    docker: tuple[Database, list[list[str]], dict[str, Any]], tmp_path: Path, tables: str
) -> None:
    database, calls, replies = docker
    source = tmp_path / "source.dump"
    source.write_bytes(b"archive")
    replies["tables"] = tables
    with pytest.raises(DatabaseError, match="database_not_empty"):
        database.restore(source)
    assert not any("cp" in call or "pg_restore" in call for call in calls)


def test_restore_is_atomic_no_owner_and_cleans_remote_on_failure(
    docker: tuple[Database, list[list[str]], dict[str, Any]], tmp_path: Path
) -> None:
    database, calls, replies = docker
    source = tmp_path / "source.dump"
    source.write_bytes(b"archive")
    replies["fail"] = "pg_restore"
    with pytest.raises(DatabaseError):
        database.restore(source)
    restore = next(call for call in calls if "pg_restore" in call)
    assert {"--single-transaction", "--exit-on-error", "--no-owner"}.issubset(restore)
    assert calls[-1][-3:-1] == ["rm", "-f"]
    assert source.read_bytes() == b"archive"


def test_backup_cli_requires_path_before_side_effects(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as captured:
        main(["backup"])
    assert captured.value.code == 2
    assert "--backup-path" in capsys.readouterr().err
