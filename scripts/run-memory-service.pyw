"""Windowless Scheduler entry point; wait for and supervise the foreground service."""

import json
import subprocess
import sys
from pathlib import Path

runtime = Path(__file__).resolve().parent
deployment = json.loads((runtime / "deployment.json").read_text("utf-8"))
with (runtime / "bootstrap.stderr.log").open("ab") as errors:
    result = subprocess.run(
        [
            deployment["pwsh"],
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(runtime / "run-memory-service.ps1"),
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=errors,
        check=False,
    )
sys.exit(result.returncode)
