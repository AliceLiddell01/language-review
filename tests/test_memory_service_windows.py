from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Scheduler entry point")


def test_windowless_entrypoint_creates_no_console_and_returns_supervisor_failure(
    tmp_path: Path,
) -> None:
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
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert marker.read_text("utf-8") == "Keep these bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["unrelated.txt"]
