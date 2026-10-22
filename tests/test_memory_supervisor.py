from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from language_review.memory.service import (
    CREDENTIAL,
    TASK_NAMESPACE,
    ServiceError,
    atomic,
    database_environment,
    database_state,
    dpapi,
    read_password,
    service_lock,
    task_xml,
    wait_ready,
    write_password,
)

WINDOWS = pytest.mark.skipif(os.name != "nt", reason="NOT_RUN: контракт Windows DPAPI/locking")


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], "absent"),
        ([{"State": "exited"}], "stopped"),
        ([{"State": "running", "Health": "healthy"}], "running"),
        ([{"State": "running", "Health": "unhealthy"}], "running"),
        ([{"State": "restarting"}], "unknown"),
        ([{"Health": "unhealthy"}], "unknown"),
    ],
)
def test_database_recovery_requires_positive_evidence_of_stopped_container(
    rows: list[dict[str, str]], expected: str
) -> None:
    assert database_state(rows) == expected


@WINDOWS
def test_credentials_use_current_user_dpapi_without_changing_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGUAGE_REVIEW_MEMORY_DSN", "parent-dsn")
    monkeypatch.setenv("LANGUAGE_REVIEW_DB_PASSWORD", "parent-password")
    password = "Синтетический пароль @:/&"
    write_password(tmp_path, password)
    ciphertext = (tmp_path / CREDENTIAL).read_bytes()
    assert password.encode() not in ciphertext
    assert dpapi(ciphertext, decrypt=True).decode() == password
    environment = database_environment(tmp_path, {"port": 54329})
    assert environment["LANGUAGE_REVIEW_DB_PASSWORD"] == password
    assert "%40%3A%2F%26" in environment["LANGUAGE_REVIEW_MEMORY_DSN"]
    assert os.environ["LANGUAGE_REVIEW_MEMORY_DSN"] == "parent-dsn"
    assert os.environ["LANGUAGE_REVIEW_DB_PASSWORD"] == "parent-password"


@WINDOWS
def test_legacy_clixml_migration_keeps_ciphertext_and_never_writes_plaintext(
    tmp_path: Path,
) -> None:
    password = "test-only-legacy-пароль"
    ciphertext = dpapi(password.encode("utf-16-le")).hex()
    legacy = (
        '<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04"><Obj><Props>'
        '<S N="UserName">language_review</S>'
        f'<SS N="Password">{ciphertext}</SS></Props></Obj></Objs>'
    ).encode()
    (tmp_path / "database.credential.xml").write_bytes(legacy)
    assert read_password(tmp_path) == password
    assert read_password(tmp_path) == password
    assert (tmp_path / "database.credential.xml").read_bytes() == legacy
    assert set(path.name for path in tmp_path.iterdir()) == {"database.credential.xml", CREDENTIAL}
    assert all(password.encode() not in path.read_bytes() for path in tmp_path.iterdir())


@WINDOWS
def test_credentials_reject_plaintext_legacy_password(tmp_path: Path) -> None:
    (tmp_path / "database.credential.xml").write_text(
        '<Objs><S N="UserName">language_review</S><S N="Password">test-only</S></Objs>'
    )
    with pytest.raises(ServiceError, match="исходные DPAPI"):
        read_password(tmp_path)
    assert not (tmp_path / CREDENTIAL).exists()


@WINDOWS
def test_service_lock_excludes_duplicate_supervisor_and_releases_handle(tmp_path: Path) -> None:
    with service_lock(tmp_path) as acquired:
        assert acquired
        with service_lock(tmp_path) as duplicate:
            assert not duplicate
    with service_lock(tmp_path) as next_supervisor:
        assert next_supervisor


def test_scheduler_xml_binds_python_task_to_runtime_and_current_user(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime with пробелы & символы"
    pythonw = tmp_path / "Python runtime/pythonw.exe"
    root = ET.fromstring(task_xml(runtime, pythonw, "S-1-5-21-123"))
    namespace = {"t": TASK_NAMESPACE}
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=namespace) == str(pythonw)
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=namespace)
    assert arguments == f'-I "{runtime / "run-memory-service.pyw"}"'
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=namespace) == str(
        runtime
    )
    assert (
        root.findtext("t:Principals/t:Principal/t:UserId", namespaces=namespace) == "S-1-5-21-123"
    )
    assert (
        root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=namespace)
        == "InteractiveToken"
    )
    assert (
        root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=namespace) == "IgnoreNew"
    )
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=namespace) == "PT0S"
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=namespace) == "999"
    assert (
        root.findtext("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=namespace)
        == "PT1M"
    )
    assert (
        root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=namespace) == "S-1-5-21-123"
    )


@pytest.mark.parametrize("status", ["idle", "processed", "partial"])
def test_readiness_accepts_only_fresh_state_of_candidate_release(
    tmp_path: Path, status: str
) -> None:
    after = datetime.now(UTC)
    release = tmp_path / "candidate"
    record = {
        "release": str(release),
        "at": (after + timedelta(seconds=1)).isoformat(),
        "status": status,
    }
    atomic(tmp_path / "logs/service-state.json", json.dumps(record).encode())
    assert wait_ready(tmp_path, release, after, 1) == record


def test_readiness_fails_immediately_on_actual_candidate_error(tmp_path: Path) -> None:
    after = datetime.now(UTC)
    release = tmp_path / "candidate"
    record = {
        "release": str(release),
        "at": (after + timedelta(seconds=1)).isoformat(),
        "status": "error",
    }
    atomic(tmp_path / "logs/service-state.json", json.dumps(record).encode())
    with pytest.raises(ServiceError, match="сообщил об ошибке worker"):
        wait_ready(tmp_path, release, after, 30)


@pytest.mark.parametrize("scenario", ["old_release", "old_timestamp", "malformed"])
def test_readiness_refuses_stale_or_malformed_state(
    tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    after = datetime.now(UTC)
    release = tmp_path / "candidate"
    record = {"release": str(release), "at": after.isoformat(), "status": "idle"}
    if scenario == "old_release":
        record.update(release="old-release", at=(after + timedelta(seconds=1)).isoformat())
    if scenario == "old_timestamp":
        record["at"] = (after - timedelta(seconds=1)).isoformat()
    payload = b"{" if scenario == "malformed" else json.dumps(record).encode()
    atomic(tmp_path / "logs/service-state.json", payload)
    clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("language_review.memory.service.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("language_review.memory.service.time.sleep", lambda _: None)
    with pytest.raises(ServiceError, match="не подтвердила готовность"):
        wait_ready(tmp_path, release, after, 1)
