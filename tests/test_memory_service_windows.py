from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Scheduler entry point")


def test_windowless_entrypoint_creates_no_console_and_returns_supervisor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POWERSHELL_UPDATECHECK", "Default")
    pwsh = shutil.which("pwsh")
    assert pwsh, "PowerShell 7 is required for Windows development"
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
    assert "Refusing to change permissions of a nonempty directory" in result.stderr.decode(
        errors="replace"
    )
    assert marker.read_text("utf-8") == "Keep these bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["unrelated.txt"]


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
    # Same actual interpreter as Scheduler: no venv redirector between it and Windows.
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
        assert kernel.WaitForSingleObject(child, 5000) == 0, "Supervisor survived its owner"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child:
            # Handle identifies this exact owned child, even if its PID is later reused.
            if kernel.WaitForSingleObject(child, 0) != 0:
                kernel.TerminateProcess(child, 1)
            kernel.CloseHandle(child)
