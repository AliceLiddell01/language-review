from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import venv
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NOT_RUN: точка входа Windows Scheduler")


@pytest.mark.parametrize("encoding", ["console", "utf-16"])
def test_scheduler_query_parses_xml_from_schtasks(
    tmp_path: Path, monkeypatch, encoding: str
) -> None:
    from language_review.memory.service import Scheduler, task_xml

    runtime = tmp_path / "runtime with spaces"
    scheduler = Scheduler(runtime, "lr-scheduler-query-test")
    definition = task_xml(runtime, tmp_path / "pythonw.exe", "S-1-5-21-123")
    response = '<?xml version="1.0" encoding="UTF-16"?>\r\r\n' + definition
    if encoding == "utf-16":
        output = response.encode("utf-16")
    else:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        codepage = kernel.GetConsoleOutputCP() or kernel.GetACP()
        output = response.encode(f"cp{codepage}")
    monkeypatch.setattr(
        scheduler,
        "_call",
        lambda *args: subprocess.CompletedProcess(args, 0, output, b""),
    )

    result = scheduler.query()

    assert result is not None
    assert result["description"] == scheduler.description
    assert result["actions"] == [
        {
            "execute": str(tmp_path / "pythonw.exe"),
            "arguments": f'-I "{runtime / "run-memory-service.pyw"}"',
            "working_directory": str(runtime),
        }
    ]


def isolated_wrapper(tmp_path: Path, module: str) -> tuple[Path, Path]:
    """Настоящие pythonw и wrapper, независимый Python child вместо shell-фикстуры."""
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    interpreter = tmp_path / "probe-python"
    venv.EnvBuilder(with_pip=False).create(interpreter)
    package = interpreter / "Lib/site-packages/language_review/memory"
    package.mkdir(parents=True)
    package.parent.joinpath("__init__.py").write_text("", encoding="utf-8")
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    package.joinpath("service.py").write_text(module, encoding="utf-8")
    wrapper = runtime / "run-memory-service.pyw"
    shutil.copyfile(Path(__file__).parents[1] / "scripts" / wrapper.name, wrapper)
    (runtime / "deployment.json").write_text(
        json.dumps({"python": str(interpreter / "Scripts/python.exe")}), encoding="utf-8"
    )
    return runtime, wrapper


def test_windowless_entrypoint_creates_no_console_and_returns_supervisor_failure(
    tmp_path: Path,
) -> None:
    runtime, wrapper = isolated_wrapper(
        tmp_path,
        """
import ctypes
import pathlib
import sys
runtime = pathlib.Path(sys.argv[sys.argv.index('--runtime-directory') + 1])
kernel = ctypes.WinDLL('kernel32')
kernel.GetConsoleWindow.restype = ctypes.c_void_p
(runtime / 'console.txt').write_text(str(kernel.GetConsoleWindow() or 0))
raise SystemExit(17)
""",
    )
    completed = subprocess.run(
        [str(Path(sys._base_executable).with_name("pythonw.exe")), "-I", str(wrapper)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 17
    assert (runtime / "console.txt").read_text() == "0"
    assert (runtime / "bootstrap.stderr.log").read_bytes() == b""
    assert not list(runtime.glob("*.ps1"))


@pytest.mark.parametrize(
    "project", ["hindsight-memory/..", "project with spaces"], ids=["traversal", "spaces"]
)
def test_installer_rejects_invalid_compose_identity_before_any_side_effect(
    tmp_path: Path, project: str
) -> None:
    runtime = tmp_path / "must-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "language_review.memory.service",
            "install",
            "--runtime-directory",
            str(runtime),
            "--project",
            project,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    stderr = result.stderr.decode(errors="replace")
    assert "usage:" not in stderr.lower()
    assert "некорректный project:" in stderr.lower()
    assert not runtime.exists()


def test_installer_preserves_unrelated_nonempty_directory(tmp_path: Path) -> None:
    marker = tmp_path / "unrelated.txt"
    marker.write_text("Keep these bytes", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "language_review.memory.service",
            "install",
            "--runtime-directory",
            str(tmp_path),
            "--project",
            "language-review-test",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "Отказ менять права непустого каталога" in result.stderr.decode(errors="replace")
    assert marker.read_text("utf-8") == "Keep these bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["unrelated.txt"]


VALID_WORKER_CONFIG = (
    '[check]\nengines = ["llm"]\n[memory]\nenabled = true\ndsn_env = "LANGUAGE_REVIEW_MEMORY_DSN"\n'
)


@pytest.mark.parametrize(
    "scenario,expected_calls,expected_state",
    [
        ("configuration", [], {"status": "error", "reason": "worker_failed", "exit_code": 2}),
        (
            "database_stopped",
            ["ps", "up"],
            {
                "status": "waiting",
                "reason": "database_unavailable",
                "database": "absent",
                "recovery": "compose_up",
                "exit_code": 6,
            },
        ),
        (
            "database_running",
            ["ps"],
            {
                "status": "error",
                "reason": "memory_database_failed",
                "database": "running",
                "exit_code": 6,
            },
        ),
    ],
    ids=["configuration", "own-stopped-database", "running-database"],
)
def test_supervisor_requests_docker_recovery_only_for_own_unavailable_database(
    tmp_path: Path, scenario: str, expected_calls: list[str], expected_state: dict[str, object]
) -> None:
    from language_review.memory.service import Supervisor, write_password

    if scenario != "configuration":
        pytest.importorskip("psycopg", reason="NOT_RUN: memory extra не установлен")
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    write_password(runtime, "test-only")
    config = runtime / "worker.toml"
    config.write_text(
        VALID_WORKER_CONFIG + ("invalid = = toml\n" if scenario == "configuration" else ""),
        encoding="utf-8",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        closed_port = listener.getsockname()[1]
    model_rows = [
        {"name": "synthetic-llm", "digest": "a" * 64},
        {"name": "synthetic-embedding", "digest": "b" * 64},
    ]
    (runtime / "deployment.json").write_text(
        json.dumps(
            {
                "project": "lr-supervisor-test",
                "port": closed_port,
                "python": sys.executable,
                "docker": "not-launched-docker",
                "config": str(config),
                "release": "test-release",
                "worker_limit": 1,
                "llm_model": model_rows[0]["name"],
                "llm_revision": model_rows[0]["digest"],
                "embedding_model": model_rows[1]["name"],
                "embedding_revision": model_rows[1]["digest"],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    class ObservedDatabase:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert args[1] == "lr-supervisor-test"

        def status(self) -> list[dict[str, str]]:
            calls.append("ps")
            return (
                [{"Service": "postgres", "State": "running", "Health": "healthy"}]
                if scenario == "database_running"
                else []
            )

        def up(self, wait_timeout: int) -> None:
            assert wait_timeout == 30
            calls.append("up")

    supervisor = Supervisor(
        runtime, database_factory=ObservedDatabase, models_loader=lambda: model_rows
    )
    supervisor.cycle()
    state = json.loads((runtime / "logs/service-state.json").read_text("utf-8"))
    assert {key: state.get(key) for key in expected_state} == expected_state
    assert calls == expected_calls
    assert "test-only" not in json.dumps(state)
    assert "test-only" not in (runtime / "logs/worker.stderr.log").read_text("utf-8")
    assert isinstance(state["supervisor_created_at"], int)


def test_terminating_actual_windowless_wrapper_kills_supervisor_child(tmp_path: Path) -> None:
    from language_review.memory.service import write_password

    source = Path(__file__).parents[1] / "src/language_review/memory/service.py"
    runtime, wrapper = isolated_wrapper(
        tmp_path,
        f"""
import json
import os
import pathlib
import runpy
import subprocess
import sys
runtime = pathlib.Path(sys.argv[sys.argv.index('--runtime-directory') + 1])
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])
(runtime / 'children.json').write_text(json.dumps([os.getpid(), child.pid]))
service = runpy.run_path({str(source)!r})
supervisor = service['Supervisor'](runtime, database_factory=lambda *args, **kwargs: None,
                                   models_loader=lambda: [])
raise SystemExit(supervisor.run())
""",
    )
    deployment_path = runtime / "deployment.json"
    deployment = json.loads(deployment_path.read_text("utf-8"))
    deployment.update(
        {
            "project": "lr-supervisor-test",
            "port": 1,
            "docker": "not-launched",
            "release": "test-release",
        }
    )
    deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
    write_password(runtime, "test-only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    process = subprocess.Popen(
        [str(Path(sys._base_executable).with_name("pythonw.exe")), "-I", str(wrapper)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    handles: list[Any] = []
    try:
        deadline = time.monotonic() + 30
        children: list[int] = []
        while time.monotonic() < deadline and process.poll() is None:
            try:
                children = json.loads((runtime / "children.json").read_text("utf-8"))
                break
            except OSError, ValueError:
                time.sleep(0.1)
        assert len(children) == 2, (runtime / "bootstrap.stderr.log").read_text("utf-8")
        while time.monotonic() < deadline:
            opened: list[Any] = []
            for pid in children:
                handle = kernel.OpenProcess(0x100001, False, pid)
                if handle and kernel.WaitForSingleObject(handle, 0) == 0x102:
                    opened.append(handle)
                elif handle:
                    kernel.CloseHandle(handle)
            if len(opened) == len(children):
                handles = opened
                break
            for handle in opened:
                kernel.CloseHandle(handle)
            if process.poll() is not None:
                break
            time.sleep(0.1)
        else:
            pytest.fail(
                "Настоящий supervisor и его потомок не остались запущенными: "
                f"код wrapper={process.poll()}, children={children}, "
                f"stderr={(runtime / 'bootstrap.stderr.log').read_text('utf-8')}"
            )
        assert len(handles) == len(children), "Не удалось открыть оба живых процесса"
        assert process.poll() is None, f"Wrapper неожиданно завершился: {process.returncode}"
        process.terminate()
        process.wait(timeout=5)
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, 10000) == 0, (
                "Потомок пережил своего владельца"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)
