"""Приёмка установленного Windows worker с собственными task/БД и настоящей Ollama."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from language_review.memory.database import Database


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8-sig"))
        return dict(value) if isinstance(value, dict) else {}
    except OSError, TypeError, ValueError:
        return {}


def wait_state(
    path: Path, release: str, statuses: set[str], after: str, seconds: int
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        current = read_json(path)
        if (
            current.get("release") == release
            and current.get("status") in statuses
            and current.get("at", "") > after
        ):
            return current
        time.sleep(1)
    return {}


def owned_process(pid: int, runtime: Path, created_at: int) -> Any:
    """Открытый handle и точная argv защищают от чужого процесса и повторного PID."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.OpenProcess(0x100001 | 0x0400, False, pid)
    if not handle:
        raise RuntimeError("Не удалось открыть собственный supervisor.")
    try:
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            *([ctypes.POINTER(wintypes.FILETIME)] * 4),
        ]
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise RuntimeError("Не удалось проверить время создания supervisor.")
        actual_creation = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if actual_creation != created_at:
            raise RuntimeError("PID supervisor уже принадлежит другому процессу.")

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.USHORT),
                ("maximum", wintypes.USHORT),
                ("buffer", ctypes.c_void_p),
            ]

        query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
        query.argtypes = [
            wintypes.HANDLE,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        query.restype = wintypes.LONG
        size = wintypes.ULONG()
        query(handle, 60, None, 0, ctypes.byref(size))
        if size.value == 0 or size.value > 1024 * 1024:
            raise RuntimeError("Не удалось прочитать идентичность supervisor.")
        buffer = ctypes.create_string_buffer(size.value)
        if query(handle, 60, buffer, size.value, ctypes.byref(size)) < 0:
            raise RuntimeError("Не удалось подтвердить идентичность supervisor.")
        value = UnicodeString.from_buffer(buffer)
        commandline = ctypes.wstring_at(value.buffer, value.length // 2)
        shell_api = ctypes.WinDLL("shell32", use_last_error=True)
        shell_api.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell_api.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        argc = ctypes.c_int()
        argv = shell_api.CommandLineToArgvW(commandline, ctypes.byref(argc))
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        try:
            arguments = [argv[index] for index in range(argc.value)]
        finally:
            kernel.LocalFree(argv)
        expected = [
            "-I",
            "-m",
            "language_review.memory.service",
            "supervise",
            "--runtime-directory",
            str(runtime),
        ]
        if arguments[1:] != expected:
            raise RuntimeError("Отказ завершать процесс с чужой командной строкой.")
        return kernel, handle
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-directory", type=Path)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--llm-model", default="qwen3.5:4b")
    parser.add_argument("--embedding-model", default="embeddinggemma:300m")
    parser.add_argument(
        "--embedding-revision",
        default="85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1",
    )
    parser.add_argument("--embedding-dimensions", type=int, default=768)
    arguments = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Приёмка Windows Scheduler требует нативную Windows.")
    from language_review.memory.service import Scheduler, database_environment

    repo = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix="lr-service-")).resolve()
    runtime = scratch / "runtime with spaces"
    project = "lr-service-test-" + uuid4().hex[:12]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = dict(os.environ)
    for name in (
        "LANGUAGE_REVIEW_MEMORY_DSN",
        "LANGUAGE_REVIEW_DB_PASSWORD",
        "LANGUAGE_REVIEW_DB_PORT",
        "LANGUAGE_REVIEW_TEST_INSTALL_FAULT",
    ):
        environment.pop(name, None)
    summary: dict[str, Any] = {
        "status": "FAIL",
        "project": project,
        "port": port,
        "reboot": "NOT_RUN",
        "checks": [],
        "cleanup_status": "NOT_RUN",
        "cleanup_errors": [],
    }
    deployment: dict[str, Any] = {}
    manifest_file = runtime / "deployment.json"
    state_file = runtime / "logs/service-state.json"
    scheduler = Scheduler(runtime, project)

    def check(name: str, condition: Any) -> None:
        if not condition:
            raise RuntimeError("FAIL: " + name)
        summary["checks"].append(name)

    def command(
        args: list[str],
        name: str,
        *,
        env: dict[str, str] | None = None,
        codes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            env=environment if env is None else env,
            cwd=repo,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Только собственные синтетические входы, DSN/password не добавляются к argv.
        password = (env or environment).get("LANGUAGE_REVIEW_DB_PASSWORD", "")
        for suffix, content in (("stdout", result.stdout), ("stderr", result.stderr)):
            (scratch / f"{name}.{suffix}.log").write_text(
                content.replace(password, "[REDACTED]") if password else content, encoding="utf-8"
            )
        if result.returncode not in codes:
            raise RuntimeError(f"Проверка {name}: код {result.returncode}; см. журнал.")
        return result

    def install(fault: str = "") -> str:
        env = {**environment, "LANGUAGE_REVIEW_TEST_INSTALL_FAULT": fault}
        result = command(
            [
                sys.executable,
                "-m",
                "language_review.memory.service",
                "install",
                "--runtime-directory",
                str(runtime),
                "--project",
                project,
                "--port",
                str(port),
                "--llm-model",
                arguments.llm_model,
                "--embedding-model",
                arguments.embedding_model,
                "--embedding-revision",
                arguments.embedding_revision,
                "--embedding-dimensions",
                str(arguments.embedding_dimensions),
                *(
                    ["--release-directory", str(arguments.release_directory)]
                    if arguments.release_directory
                    else []
                ),
            ],
            "install-" + (fault or "success"),
            env=env,
            codes=(1,) if fault else (0,),
        )
        return result.stderr

    def cli(args: list[str], codes: tuple[int, ...] = (0,)) -> dict[str, Any]:
        cli_environment = database_environment(runtime, deployment)
        for name in ("APPDATA", "XDG_CONFIG_HOME"):
            if name in environment:
                cli_environment[name] = environment[name]
        result = command(
            [
                deployment["python"],
                "-I",
                "-m",
                "language_review",
                "--config",
                deployment["config"],
                *args,
            ],
            "cli",
            env=cli_environment,
            codes=codes,
        )
        return dict(json.loads(result.stdout))

    def processed(project_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 300
        database_retry_deadline: float | None = None
        while time.monotonic() < deadline:
            try:
                status = cli(["memory", "status", "--project", project_id])
            except RuntimeError:
                error = (scratch / "cli.stderr.log").read_text("utf-8", errors="replace")
                if "memory_database_failed" not in error:
                    raise
                if database_retry_deadline is None:
                    database_retry_deadline = time.monotonic() + 60
                if time.monotonic() >= database_retry_deadline:
                    raise
                time.sleep(2)
                continue
            database_retry_deadline = None
            jobs = status.get("jobs", [])
            if any(job["state"] == "failed" for job in jobs):
                raise RuntimeError("Фоновый обработчик исчерпал попытки.")
            index = status["index"]
            if (
                jobs
                and all(job["state"] == "done" for job in jobs)
                and index["rules"] > 0
                and index["indexed"] == index["rules"]
            ):
                return status
            time.sleep(2)
        raise RuntimeError("Истекло время автоматической обработки фоновым обработчиком.")

    def records() -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (runtime / "logs/service.jsonl").read_text("utf-8").splitlines()
            if line
        ]

    try:
        message = install("start")
        check(
            "failed_first_install_leaves_no_task_or_manifest",
            "Установка не выполнена" in message
            and scheduler.query() is None
            and not manifest_file.exists()
            and (runtime / "database.credential.dpapi").is_file(),
        )
        install()
        deployment = read_json(manifest_file)
        check(
            "installed_release_independent_of_checkout",
            Path(deployment["python"]).is_relative_to(runtime)
            and not deployment["source_dirty"]
            and deployment["status"] == "installed",
        )
        environment["APPDATA"] = str(scratch / "empty-appdata")
        environment["XDG_CONFIG_HOME"] = environment["APPDATA"]
        definition = scheduler.query()
        assert definition is not None
        xml = ET.fromstring(definition["xml"])
        ns = {"s": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        check(
            "current_user_logon_trigger_and_restart_policy",
            xml.find("s:Triggers/s:LogonTrigger", ns) is not None
            and xml.findtext("s:Principals/s:Principal/s:LogonType", namespaces=ns)
            == "InteractiveToken"
            and xml.findtext("s:Settings/s:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
            and xml.findtext("s:Settings/s:ExecutionTimeLimit", namespaces=ns) == "PT0S"
            and xml.findtext("s:Settings/s:RestartOnFailure/s:Count", namespaces=ns) == "999"
            and any(
                node.text == "PT1M"
                for node in xml.findall("s:Triggers/*/s:Repetition/s:Interval", ns)
            ),
        )
        actions = definition["actions"]
        check(
            "scheduled_action_is_python_without_shell",
            len(actions) == 1
            and Path(actions[0]["execute"]).name.lower() == "pythonw.exe"
            and "run-memory-service.pyw" in actions[0]["arguments"],
        )
        forbidden = {".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".bat", ".cmd"}
        check(
            "installed_runtime_contains_no_shell_automation",
            not any(
                path.suffix.lower() in forbidden for path in runtime.rglob("*") if path.is_file()
            ),
        )
        db_env = database_environment(runtime, deployment)
        database = Database(
            runtime / "compose.memory.yml", project, environment=db_env, docker=deployment["docker"]
        )
        container = json.loads(
            command(
                [deployment["docker"], "inspect", f"{project}-postgres-1"], "inspect", env=db_env
            ).stdout
        )[0]
        check(
            "own_volume_loopback_port_and_restart_policy",
            container["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
            and any(mount.get("Name") == f"{project}_memory-data" for mount in container["Mounts"])
            and container["HostConfig"]["PortBindings"]["5432/tcp"][0]
            == {"HostIp": "127.0.0.1", "HostPort": str(port)},
        )
        sample = scratch / "собственные данные.md"
        sample.write_text(
            "Нужно праверить параметр конфигурации. Этот параметр возврощает результат проверки.",
            encoding="utf-8",
        )
        policy = {
            "schema_version": 1,
            "default_profile": "acceptance",
            "profiles": {
                "acceptance": {
                    "engines": ["llm"],
                    "llm": {"provider": "ollama", "model": arguments.llm_model},
                }
            },
        }
        (scratch / ".language-review.yaml").write_text(json.dumps(policy), encoding="utf-8")
        source_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
        project_id = str(uuid4())

        def capture(identifier: str) -> dict[str, Any]:
            return cli(
                [
                    "check",
                    str(sample),
                    "--memory-project",
                    identifier,
                    "--memory-dataset",
                    "synthetic",
                    "--view",
                    "review",
                    "--format",
                    "json",
                ],
                (0, 1, 4),
            )

        report = capture(project_id)
        check(
            "real_check_automatically_enqueues_observations",
            report.get("schema") == "language-review.review" and report.get("items"),
        )
        status = processed(project_id)
        (scratch / "processed.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        check(
            "scheduled_worker_automatically_consolidates_and_indexes",
            status["latest_consolidation"]["model"] == arguments.llm_model
            and status["index"]["indexed"] > 0,
        )
        check(
            "hypotheses_do_not_become_human_decisions",
            all(finding["state"] == "unverified" for finding in status["findings"]),
        )
        check(
            "source_bytes_unchanged", hashlib.sha256(sample.read_bytes()).hexdigest() == source_hash
        )
        before = read_json(state_file)
        kernel, handle = owned_process(
            int(before["supervisor_pid"]), runtime, int(before["supervisor_created_at"])
        )
        try:
            check("owned_supervisor_identity", True)
            check("crash_owned_supervisor", kernel.TerminateProcess(handle, 1))
            check("crashed_supervisor_exited", kernel.WaitForSingleObject(handle, 10000) == 0)
        finally:
            kernel.CloseHandle(handle)
        restarted = wait_state(
            state_file, deployment["release"], {"idle", "processed", "partial"}, now(), 110
        )
        check(
            "scheduler_restarts_crashed_supervisor",
            restarted and restarted["supervisor_pid"] != before["supervisor_pid"],
        )
        second_project = str(uuid4())
        capture(second_project)
        check(
            "new_project_processed_after_restart_without_manual_work",
            processed(second_project)["index"]["indexed"] > 0,
        )
        manifest_bytes = manifest_file.read_bytes()
        config = Path(deployment["config"])
        config_bytes = config.read_bytes()
        for fault in ("register", "start", "readiness"):
            failed_at = now()
            message = install(fault)
            restored = scheduler.query()
            resumed = wait_state(
                state_file, deployment["release"], {"idle", "processed", "partial"}, failed_at, 120
            )
            check(
                f"failed_update_{fault}_restores_previous_release",
                "Обновление не выполнено" in message
                and "восстановлены" in message
                and "Откат выполнен не полностью" not in message
                and manifest_file.read_bytes() == manifest_bytes
                and config.read_bytes() == config_bytes
                and restored
                and restored["enabled"]
                and restored["description"] == f"Language Review memory: {runtime}"
                and resumed,
            )
        check(
            "previous_worker_keeps_processing_after_rollback",
            capture(project_id)["schema"] == "language-review.review",
        )
        persisted_before = processed(project_id)
        releases = {deployment["release"]}
        for attempt in (1, 2):
            install()
            deployment = read_json(manifest_file)
            check(
                f"successful_update_{attempt}_publishes_ready_release",
                deployment["status"] == "installed"
                and deployment["release"] not in releases
                and read_json(state_file).get("release") == deployment["release"],
            )
            releases.add(deployment["release"])
        config = Path(deployment["config"])
        config_backup = config.read_bytes()
        broken = now()
        config.write_bytes(config_backup + b"\ninvalid = = toml\n")
        failed = wait_state(state_file, deployment["release"], {"error"}, broken, 90)
        check(
            "worker_configuration_error_is_visible_and_not_database_recovery",
            failed.get("reason") == "worker_failed" and failed.get("exit_code") == 2,
        )
        database.stop()
        stopped = now()
        time.sleep(30)
        states = database.status()
        check(
            "non_database_error_never_starts_docker_recovery",
            states
            and all(row.get("State") != "running" for row in states)
            and not any(record.get("recovery") and record["at"] > broken for record in records())
            and read_json(state_file).get("status") == "error",
        )
        config.write_bytes(config_backup)
        deadline = time.monotonic() + 240
        recovered = False
        while time.monotonic() < deadline:
            states = database.status()
            recovered = bool(states) and all(
                row.get("State") == "running" and row.get("Health") == "healthy" for row in states
            )
            if recovered:
                break
            time.sleep(2)
        check("supervisor_recovers_own_database_without_data_loss", recovered)
        recovery = [
            record
            for record in records()
            if record.get("recovery") == "compose_up" and record["at"] > stopped
        ]
        check(
            "database_recovery_is_reported_as_database_unavailable",
            recovery and recovery[0].get("reason") == "database_unavailable",
        )
        check(
            "original_events_persist_across_database_restart",
            processed(project_id)["durable_events"] == persisted_before["durable_events"],
        )
        owned = read_json(state_file)
        kernel, handle = owned_process(
            int(owned["supervisor_pid"]), runtime, int(owned["supervisor_created_at"])
        )
        try:
            scheduler.stop()
            check(
                "stopping_task_terminates_supervisor_tree",
                kernel.WaitForSingleObject(handle, 15000) == 0,
            )
        finally:
            kernel.CloseHandle(handle)
        scheduler.start()
        summary.update(
            {
                "status": "PASS",
                **{
                    name: deployment[name]
                    for name in ("source_sha", "wheel_sha256", "llm_revision", "embedding_revision")
                },
            }
        )
    except Exception as error:
        # Runtime errors содержат только имена проверок; произвольные исключения — только тип.
        summary["failure"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        removable = True
        try:
            task = scheduler.query()
            if task:
                if task["description"] != f"Language Review memory: {runtime}":
                    raise RuntimeError("Чужая задача сохранена.")
                scheduler.stop()
                scheduler.remove()
                time.sleep(2)
        except Exception as error:
            summary["cleanup_errors"].append("Задача: " + type(error).__name__)
            removable = False
        if removable:
            try:
                if not re.fullmatch(r"lr-service-test-[0-9a-f]{12}", project):
                    raise RuntimeError("Небезопасный project.")
                compose = runtime / "compose.memory.yml"
                if not compose.is_file():
                    compose = repo / "compose.memory.yml"
                cleanup_env = {
                    **environment,
                    "LANGUAGE_REVIEW_DB_PASSWORD": "cleanup-only",
                    "LANGUAGE_REVIEW_DB_PORT": str(port),
                }
                Database(compose, project, environment=cleanup_env).down(remove_volumes=True)
            except Exception as error:
                summary["cleanup_errors"].append("Compose: " + type(error).__name__)
                removable = False
        if removable:
            try:
                expected = scratch.resolve() / "runtime with spaces"
                if (
                    runtime.resolve() != expected
                    or not expected.is_relative_to(Path(tempfile.gettempdir()).resolve())
                    or runtime.is_symlink()
                    or runtime.is_junction()
                ):
                    raise RuntimeError("Небезопасный путь runtime.")
                if runtime.exists():
                    shutil.rmtree(runtime)
            except Exception as error:
                summary["cleanup_errors"].append("Runtime: " + type(error).__name__)
        summary["cleanup_status"] = "FAIL" if summary["cleanup_errors"] else "PASS"
        if summary["cleanup_errors"]:
            summary["status"] = "FAIL"
        serialized = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        try:
            (scratch / "acceptance.json").write_text(serialized, encoding="utf-8")
            if arguments.result_file:
                arguments.result_file.write_text(serialized, encoding="utf-8")
        except OSError:
            summary["status"] = "FAIL"
            print("Не удалось записать отчёт приёмки.", file=sys.stderr)
        print(f"Артефакты приёмки службы: {scratch}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
