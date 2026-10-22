"""Самостоятельный Windows bootstrap и его воспроизводимая сборка."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


def build(output: Path) -> None:
    if sys.platform != "win32" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("Windows bootstrap собирается только в Windows x64.")
    from importlib.metadata import version

    if version("pyinstaller") != "6.22.3":
        raise RuntimeError("Сборка требует закреплённый PyInstaller 6.22.3.")
    output = output.absolute()
    if output.exists():
        raise RuntimeError("Файл bootstrap уже существует; перезапись запрещена.")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[1] / "src"
    with tempfile.TemporaryDirectory(prefix="language-review-freeze-") as directory:
        temporary = Path(directory)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onefile",
                "--console",
                "--name",
                "install",
                "--paths",
                str(source_root),
                "--distpath",
                str(temporary / "dist"),
                "--workpath",
                str(temporary / "work"),
                "--specpath",
                str(temporary / "spec"),
                str(Path(__file__).resolve()),
            ],
            check=True,
            env={**os.environ, "PYTHONHASHSEED": "0"},
        )
        import shutil

        sys.path.insert(0, str(source_root))
        from language_review.distribution import verify_x64_executable

        verify_x64_executable(temporary / "dist/install.exe")
        shutil.copyfile(temporary / "dist/install.exe", output)


if __name__ == "__main__":
    if not getattr(sys, "frozen", False) and sys.argv[1:2] == ["build"]:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("build")
        parser.add_argument("--output", type=Path, required=True)
        build(parser.parse_args().output)
    else:
        from language_review.bootstrap import main

        raise SystemExit(main())
