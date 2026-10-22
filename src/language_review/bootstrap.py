"""Bootstrap Windows x64 без системного Python, checkout и shell-сценариев."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from language_review import distribution as deployment
from language_review.release_contract import ReleaseError, digest, read_json, validate_bundle
from language_review.release_transport import download, fetch_bundle

UV_VERSION = "0.13.1"


def obtain_uv(directory: Path, root: Path) -> str:
    candidate = shutil.which("uv")
    if not candidate and (root / "installation.json").exists():
        current = deployment.state(root)
        if current.get("active") and not current.get("uninstalled"):
            candidate = deployment.private_uv(root, current)
    if candidate:
        reported = deployment.execute([candidate, "--version"])
        match = re.match(r"uv (\d+)\.(\d+)\.(\d+)(?:\s|$)", reported)
        if not match or tuple(map(int, match.groups())) < (0, 13, 1):
            raise ReleaseError("Обновите uv до 0.13.1 или новее.")
        return candidate
    metadata = directory / "uv-release.json"
    download(
        "https://api.github.com/repos/astral-sh/uv/releases/tags/" + UV_VERSION,
        metadata,
        limit=1_000_000,
    )
    release = read_json(metadata)
    name = "uv-x86_64-pc-windows-msvc.zip"
    matches = [item for item in release.get("assets", []) if item.get("name") == name]
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{name}"
    if (
        len(matches) != 1
        or matches[0].get("browser_download_url") != url
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(matches[0].get("digest", "")))
    ):
        raise ReleaseError("Нет проверяемой поставки uv; используйте официальную поставку.")
    archive = directory / "uv.zip"
    download(url, archive, matches[0]["digest"][7:])
    with zipfile.ZipFile(archive) as package:
        entries = [entry for entry in package.infolist() if entry.filename == "uv.exe"]
        if len(entries) != 1 or entries[0].file_size > 100_000_000:
            raise ReleaseError("Неожиданный архив uv.")
        executable = directory / "uv.exe"
        with executable.open("xb") as stream:
            stream.write(package.read(entries[0]))
    deployment.verify_x64_executable(executable)
    return str(executable)


def launch(root: Path, kind: str, arguments: list[str]) -> int:
    import msvcrt

    root = root.absolute()
    lease: Any = None
    lease_path: Path | None = None
    try:
        with deployment.launch_locked(root):
            current = deployment.state(root)
            if current.get("uninstalled") or not current.get("active"):
                raise ReleaseError("Управляемая установка удалена или не завершена.")
            release = root / "releases" / current["active"]
            deployment.safe_path(release)
            protocol = release / ".run-leases/protocol"
            deployment.safe_path(protocol)
            deployment.safe_path(release / "owned-files.json")
            index = read_json(release / "owned-files.json")
            recorded = index.get("files", {}).get(str(Path(".run-leases") / "protocol"))
            if index.get("owner") != deployment.OWNER or recorded != digest(protocol):
                raise ReleaseError("Runtime не поддерживает безопасное управление lease.")
            lease_path = protocol.parent / (uuid.uuid4().hex + ".lock")
            deployment.safe_path(lease_path)
            lease = lease_path.open("x+b")
            lease.write(str(os.getpid()).encode("ascii"))
            lease.flush()
            lease.seek(0)
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            python = deployment.python_at(release)
            deployment.safe_path(python)
        environment = {**os.environ, "LANGUAGE_REVIEW_LAUNCHER_PID": str(os.getpid())}
        module = "language_review" if kind == "review" else "language_review.evaluation"
        with subprocess.Popen(
            [str(python), "-I", "-m", module, *arguments], env=environment
        ) as process:
            while True:
                try:
                    return process.wait()
                except KeyboardInterrupt:
                    # Ctrl+C доставляется также ребёнку консоли. Lease остаётся
                    # захваченным, пока настоящий CLI не завершится.
                    continue
    finally:
        if lease is not None:
            with contextlib.suppress(OSError):
                lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
            lease.close()
        if lease_path is not None:
            lease_path.unlink(missing_ok=True)


def install(arguments: argparse.Namespace) -> int:
    root = arguments.root.absolute()
    with tempfile.TemporaryDirectory(prefix="language-review-bootstrap-") as directory:
        temporary = Path(directory)
        bundle = temporary / "bundle"
        bundle.mkdir()
        if arguments.bundle_directory:
            source = arguments.bundle_directory.absolute()
            deployment.safe_path(source)
            manifest = source / "release.json"
            if (
                not re.fullmatch(r"[a-f0-9]{64}", arguments.trusted_manifest_sha256 or "")
                or digest(manifest) != arguments.trusted_manifest_sha256
            ):
                raise ReleaseError("Приёмка требует явно доверенный SHA-256 локального manifest.")
            validate_bundle(source)
            for item in source.iterdir():
                deployment.safe_path(item)
                if not item.is_file():
                    raise ReleaseError("Комплект содержит неожиданный каталог.")
                shutil.copyfile(item, bundle / item.name)
        else:
            if arguments.trusted_manifest_sha256:
                raise ReleaseError("Доверенный digest применяется только с --bundle-directory.")
            fetch_bundle(bundle, arguments.tag)
        validate_bundle(bundle)
        uv = obtain_uv(temporary, root)
        deployment.install(
            bundle,
            root,
            uv=uv,
            extras=arguments.extras,
            agents=[name for name in ("codex", "claude") if getattr(arguments, name)],
            add_path=False if arguments.no_path else None,
        )
        current = deployment.state(root)
        command = [
            str(deployment.python_at(root / "releases" / current["active"])),
            "-I",
            "-m",
            "language_review",
            "setup",
        ]
        if arguments.non_interactive:
            command.append("--non-interactive")
        result = subprocess.call(command)
        if result:
            raise ReleaseError(
                "Ядро установлено, setup не завершён; повторите language-review setup."
            )
    return 0


def main() -> int:
    try:
        if sys.platform != "win32" or platform.machine().casefold() not in {"amd64", "x86_64"}:
            raise ReleaseError("Поддерживается Windows x64.")
        # Bootloader PyInstaller меняет DLL search path; uv и внешний Python должны
        # загружать библиотеки собственной поставки.
        if getattr(sys, "frozen", False):
            import ctypes

            ctypes.windll.kernel32.SetDllDirectoryW(None)
        executable = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
        if executable.name in {"language-review.exe", "language-review-eval.exe"}:
            return launch(
                executable.parent.parent,
                "eval" if executable.name == "language-review-eval.exe" else "review",
                sys.argv[1:],
            )
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--tag")
        parser.add_argument("--root", type=Path, default=deployment.default_root())
        parser.add_argument("--extras", choices=("core", "chatgpt", "memory", "all"))
        parser.add_argument("--codex", action="store_true")
        parser.add_argument("--claude", action="store_true")
        parser.add_argument("--non-interactive", action="store_true")
        parser.add_argument("--no-path", action="store_true")
        parser.add_argument("--bundle-directory", type=Path)
        parser.add_argument("--trusted-manifest-sha256")
        parser.add_argument("--cleanup", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument(
            "--wait-for-pid", type=int, action="append", default=[], help=argparse.SUPPRESS
        )
        parser.add_argument("--launch", choices=("review", "eval"), help=argparse.SUPPRESS)
        arguments, forward = parser.parse_known_args()
        if arguments.launch:
            return launch(
                arguments.root, arguments.launch, forward[1:] if forward[:1] == ["--"] else forward
            )
        if forward:
            parser.error("Неизвестные аргументы: " + " ".join(forward))
        if arguments.cleanup:
            result = deployment.cleanup(arguments.root, wait_pids=arguments.wait_for_pid)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "uninstalled" else 1
        return install(arguments)
    except (ReleaseError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print("Установка/запуск не завершены: " + str(exc), file=sys.stderr)
        return 1
