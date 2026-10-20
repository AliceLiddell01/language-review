from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NOT_RUN: точка входа Windows Scheduler")


def test_windowless_entrypoint_creates_no_console_and_returns_supervisor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POWERSHELL_UPDATECHECK", "Default")
    pwsh = shutil.which("pwsh")
    assert pwsh, "Для разработки в Windows нужен PowerShell 7"
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    wrapper = runtime / "run-memory-service.pyw"
    shutil.copyfile(Path(__file__).parents[1] / "scripts" / wrapper.name, wrapper)
    (runtime / "deployment.json").write_text(json.dumps({"pwsh": pwsh}), encoding="utf-8")
    (runtime / "run-memory-service.ps1").write_text(
        """
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ServiceConsoleProbe {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
}
'@
$handle = [ServiceConsoleProbe]::GetConsoleWindow().ToInt64().ToString()
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'console.txt'), $handle)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'updatecheck.txt'), $env:POWERSHELL_UPDATECHECK)
exit 17
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(Path(sys.executable).with_name("pythonw.exe")), "-I", str(wrapper)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 17
    assert (runtime / "console.txt").read_text() == "0"
    assert (runtime / "bootstrap.stderr.log").read_bytes() == b""
    assert (runtime / "updatecheck.txt").read_text() == "Off"
    assert os.environ["POWERSHELL_UPDATECHECK"] == "Default"


@pytest.mark.parametrize("project", ["hindsight-memory/..", "project with spaces"])
def test_installer_rejects_invalid_compose_identity_before_any_side_effect(
    tmp_path: Path, project: str
) -> None:
    runtime = tmp_path / "must-not-exist"
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(Path(__file__).parents[1] / "scripts" / "install-memory-service.ps1"),
            "-RuntimeDirectory",
            str(runtime),
            "-Project",
            project,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    stderr = result.stderr.decode(errors="replace")
    assert "Project" in stderr
    assert project in stderr
    assert not runtime.exists()


def test_installer_preserves_unrelated_nonempty_directory(tmp_path: Path) -> None:
    marker = tmp_path / "unrelated.txt"
    marker.write_text("Keep these bytes", encoding="utf-8")
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(Path(__file__).parents[1] / "scripts" / "install-memory-service.ps1"),
            "-RuntimeDirectory",
            str(tmp_path),
            "-Project",
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


def _ollama_models() -> list[dict[str, str]]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            return list(json.load(response)["models"])
    except OSError, ValueError, KeyError:
        return []


VALID_WORKER_CONFIG = (
    '[check]\nengines = ["llm"]\n[memory]\nenabled = true\ndsn_env = "LANGUAGE_REVIEW_MEMORY_DSN"\n'
)


@pytest.mark.parametrize(
    "scenario,expected_calls,expected_state",
    [
        # Ошибка конфигурации: worker завершается кодом 2, Docker не вызывается вовсе.
        ("configuration", [], {"status": "error", "reason": "worker_failed", "exit_code": 2}),
        # Своя PostgreSQL не запущена по данным самого Docker: только тогда compose up.
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
        # Ошибка БД при работающем healthy контейнере не является поводом для recovery.
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
)
def test_supervisor_requests_docker_recovery_only_for_own_unavailable_database(
    tmp_path: Path,
    scenario: str,
    expected_calls: list[str],
    expected_state: dict[str, object],
) -> None:
    if scenario != "configuration":
        pytest.importorskip("psycopg", reason="NOT_RUN: memory extra не установлен")
    models = _ollama_models()
    if not models:
        pytest.skip("NOT_RUN: локальная Ollama недоступна для проверки моделей supervisor")
    pwsh = shutil.which("pwsh")
    assert pwsh
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    scripts = Path(__file__).parents[1] / "scripts"
    for name in ("run-memory-service.ps1", "connect-memory.ps1"):
        shutil.copyfile(scripts / name, runtime / name)
    subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-Command",
            "[pscredential]::new('language_review', (ConvertTo-SecureString 'test-only' "
            "-AsPlainText -Force)) | Export-Clixml -LiteralPath '"
            + str(runtime / "database.credential.xml").replace("'", "''")
            + "'",
        ],
        check=True,
        timeout=60,
    )
    # Поддельный docker только фиксирует вызовы; настоящий CLI классифицирует отказ сам.
    (runtime / "docker.cmd").write_text(
        '@echo off\r\n>>"%~dp0docker-calls.txt" echo %6\r\n'
        'if "%6"=="ps" if exist "%~dp0ps.json" type "%~dp0ps.json"\r\n',
        encoding="utf-8",
    )
    if scenario == "database_running":
        (runtime / "ps.json").write_text(
            '{"Service":"postgres","State":"running","Health":"healthy"}\n', encoding="utf-8"
        )
    config = runtime / "worker.toml"
    config.write_text(
        VALID_WORKER_CONFIG + ("invalid = = toml\n" if scenario == "configuration" else ""),
        encoding="utf-8",
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    closed_port = listener.getsockname()[1]
    listener.close()
    (runtime / "deployment.json").write_text(
        json.dumps(
            {
                "project": "lr-supervisor-test",
                "port": closed_port,
                "python": sys.executable,
                "docker": str(runtime / "docker.cmd"),
                "config": str(config),
                "release": "test-release",
                "worker_limit": 1,
                "llm_model": models[0]["name"],
                "llm_revision": models[0]["digest"],
                "embedding_model": models[-1]["name"],
                "embedding_revision": models[-1]["digest"],
            }
        ),
        encoding="utf-8",
    )
    state_file = runtime / "logs" / "service-state.json"
    process = subprocess.Popen(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(runtime / "run-memory-service.ps1"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    state: dict[str, object] = {}
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and process.poll() is None:
            try:
                state = json.loads(state_file.read_text("utf-8-sig"))
            except OSError, ValueError:
                state = {}
            if state.get("status") not in {None, "starting"}:
                break
            time.sleep(0.2)
    finally:
        process.kill()
        _, stderr = process.communicate(timeout=30)
    assert {key: state.get(key) for key in expected_state} == expected_state, stderr.decode(
        errors="replace"
    )
    calls = runtime / "docker-calls.txt"
    recorded = calls.read_text("utf-8").split() if calls.exists() else []
    assert recorded == expected_calls
    assert "test-only" not in json.dumps(state)


def test_terminating_actual_windowless_wrapper_kills_supervisor_child(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    pwsh = shutil.which("pwsh")
    assert pwsh
    wrapper = tmp_path / "run-memory-service.pyw"
    shutil.copyfile(Path(__file__).parents[1] / "scripts" / wrapper.name, wrapper)
    (tmp_path / "deployment.json").write_text(json.dumps({"pwsh": pwsh}), encoding="utf-8")
    (tmp_path / "run-memory-service.ps1").write_text(
        "[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'child.txt'), [string]$PID)\n"
        "Start-Sleep -Seconds 300\n",
        encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child = None
    # Тот же настоящий интерпретатор, что у Scheduler: без перенаправителя venv.
    process = subprocess.Popen(
        [str(Path(sys._base_executable).with_name("pythonw.exe")), "-I", str(wrapper)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 20
        child_file = tmp_path / "child.txt"
        child_pid = ""
        while time.monotonic() < deadline and process.poll() is None:
            if child_file.exists():
                child_pid = child_file.read_text().strip()
                if child_pid.isdigit():
                    break
            time.sleep(0.05)
        assert child_pid.isdigit(), (tmp_path / "bootstrap.stderr.log").read_text("utf-8")
        child = kernel.OpenProcess(0x100001, False, int(child_pid))
        assert child
        process.terminate()
        process.wait(timeout=5)
        assert kernel.WaitForSingleObject(child, 5000) == 0, "Supervisor пережил своего владельца"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child:
            # Handle указывает именно на этот дочерний процесс, даже если PID переиспользован.
            if kernel.WaitForSingleObject(child, 0) != 0:
                kernel.TerminateProcess(child, 1)
            kernel.CloseHandle(child)
