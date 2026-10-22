"""Границы нового bootstrap, миграции launcher и внешней Python-очистки."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import venv
import zipfile
from pathlib import Path
from typing import Any

import pytest
from support import create_junction

from language_review import bootstrap
from language_review import distribution as deploy
from language_review.release_contract import ReleaseError, digest


def installation(root: Path, *, uninstalled: bool = False) -> dict[str, Any]:
    current: dict[str, Any] = {
        "owner": deploy.OWNER,
        "root": str(root.absolute()),
        "active": "a" * 32,
        "uninstalled": uninstalled,
        "launchers": {},
    }
    deploy.atomic(root / "installation.json", deploy.json_bytes(current))
    return current


def owned_release(root: Path) -> Path:
    release = root / "releases" / ("a" * 32)
    protocol = release / ".run-leases/protocol"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("1\n", encoding="ascii")
    runtime = release / "owned.txt"
    runtime.write_bytes(b"owned")
    deploy.atomic(
        release / "owned-files.json",
        deploy.json_bytes(
            {
                "owner": deploy.OWNER,
                "files": {
                    str(protocol.relative_to(release)): digest(protocol),
                    "owned.txt": digest(runtime),
                },
            }
        ),
    )
    return release


def test_legacy_launcher_migration_is_one_recoverable_transaction(tmp_path: Path) -> None:
    root = tmp_path / "application"
    old = installation(root)
    for name in deploy.LEGACY_LAUNCHERS:
        path = root / "bin" / name
        deploy.atomic(path, ("прежний owned launcher " + name).encode())
        old["launchers"][name] = digest(path)
    before = deploy.json_bytes(old)
    deploy.atomic(root / "installation.json", before)
    executable = tmp_path / "install.exe"
    executable.write_bytes(b"verified-bootstrap")

    writes, hashes = deploy.launcher_writes(root, old, executable)
    assert all(writes[root / "bin" / name] is None for name in deploy.LEGACY_LAUNCHERS)
    assert set(hashes) == {"language-review.exe", "language-review-eval.exe"}
    deploy.transaction(
        root,
        {
            **writes,
            root / "installation.json": deploy.json_bytes(
                {
                    **old,
                    "launchers": hashes,
                }
            ),
        },
    )
    assert not any((root / "bin" / name).exists() for name in deploy.LEGACY_LAUNCHERS)
    assert all(path.read_bytes() == b"verified-bootstrap" for path in deploy.launcher_paths(root))
    assert not (root / ".transaction.json").exists()


@pytest.mark.parametrize("recorded", [False, True])
def test_legacy_launcher_migration_preserves_foreign_or_changed_file(
    tmp_path: Path, recorded: bool
) -> None:
    root = tmp_path / "application"
    old = installation(root)
    legacy = root / "bin" / deploy.LEGACY_LAUNCHERS[0]
    deploy.atomic(legacy, b"original")
    if recorded:
        old["launchers"][legacy.name] = digest(legacy)
    legacy.write_bytes(b"user modification")
    executable = tmp_path / "install.exe"
    executable.write_bytes(b"new")
    before = (root / "installation.json").read_bytes()
    with pytest.raises(ReleaseError, match="миграция остановлена"):
        deploy.launcher_writes(root, old, executable)
    assert legacy.read_bytes() == b"user modification"
    assert (root / "installation.json").read_bytes() == before
    assert not any(path.exists() for path in deploy.launcher_paths(root))


def test_existing_owned_native_launchers_are_immutable_during_update(tmp_path: Path) -> None:
    root = tmp_path / "application"
    old = installation(root)
    for path in deploy.launcher_paths(root):
        deploy.atomic(path, b"trusted-stable-launcher")
        old["launchers"][path.name] = digest(path)
    candidate = tmp_path / "install.exe"
    candidate.write_bytes(b"new-bootstrap-version")
    writes, hashes = deploy.launcher_writes(root, old, candidate)
    assert writes == {}
    assert hashes == old["launchers"]
    assert all(
        path.read_bytes() == b"trusted-stable-launcher" for path in deploy.launcher_paths(root)
    )


def test_installed_runtime_removes_generated_shell_activation_files(tmp_path: Path) -> None:
    release = tmp_path / "release"
    scripts = release / "venv/Scripts"
    scripts.mkdir(parents=True)
    generated = (
        "activate",
        "activate.bat",
        "activate.csh",
        "activate.fish",
        "activate.nu",
        "activate.ps1",
        "deactivate.bat",
        "pydoc.bat",
    )
    for name in generated:
        (scripts / name).write_text("activation helper", encoding="utf-8")
    python = scripts / "python.exe"
    python.write_bytes(b"runtime interpreter")

    deploy.remove_activation_scripts(release)

    assert not any((scripts / name).exists() for name in generated)
    assert python.read_bytes() == b"runtime interpreter"


def test_failed_native_launcher_publication_restores_legacy_installation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "application"
    old = installation(root)
    for name in deploy.LEGACY_LAUNCHERS:
        path = root / "bin" / name
        deploy.atomic(path, ("owned " + name).encode())
        old["launchers"][name] = digest(path)
    deploy.atomic(root / "installation.json", deploy.json_bytes(old))
    before = {path: path.read_bytes() for path in (root / "bin").iterdir()}
    executable = tmp_path / "install.exe"
    executable.write_bytes(b"verified")
    writes, _ = deploy.launcher_writes(root, old, executable)
    original = deploy.atomic

    def fail(path: Path, content: bytes) -> None:
        if path.name == "language-review-eval.exe":
            raise PermissionError("Проверяемый сбой публикации")
        original(path, content)

    monkeypatch.setattr(deploy, "atomic", fail)
    with pytest.raises(PermissionError, match="сбой публикации"):
        deploy.transaction(root, writes)
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not any(path.exists() for path in deploy.launcher_paths(root))
    assert deploy.state(root) == old
    assert not (root / ".transaction.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows x64 PE")
def test_cleanup_interpreter_rejects_modified_pe_and_runtime_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy.verify_x64_executable(Path(sys._base_executable))
    root = tmp_path / "application"
    current = installation(root, uninstalled=True)
    local = root / "python.exe"
    local.write_bytes(b"not a PE executable")
    current["cleanup_python"] = {"executable": str(local), "sha256": digest(local)}
    deploy.atomic(root / "installation.json", deploy.json_bytes(current))
    with pytest.raises(ReleaseError, match="удаляемом runtime"):
        deploy.schedule_cleanup(root)
    external = tmp_path / "changed-python.exe"
    external.write_bytes(b"not a PE executable")
    current["cleanup_python"] = {"executable": str(external), "sha256": digest(external)}
    deploy.atomic(root / "installation.json", deploy.json_bytes(current))
    with pytest.raises(ReleaseError, match="Windows x64"):
        deploy.schedule_cleanup(root)
    current["cleanup_python"] = {
        "executable": sys._base_executable,
        "sha256": "0" * 64,
    }
    deploy.atomic(root / "installation.json", deploy.json_bytes(current))
    monkeypatch.setattr(
        deploy.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Очистка не должна запускаться"),
    )
    with pytest.raises(ReleaseError, match="Базовый Python очистки изменён"):
        deploy.schedule_cleanup(root)
    assert root.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="uv managed Python использует Windows junction")
def test_cleanup_interpreter_resolves_managed_python_junction(
    tmp_path: Path, monkeypatch: Any
) -> None:
    base = Path(sys._base_executable).resolve()
    managed = tmp_path / "python/cpython-3.15-windows-x86_64-none"
    managed.parent.mkdir(parents=True)
    create_junction(managed, base.parent)
    monkeypatch.setattr(
        deploy,
        "execute",
        lambda _arguments: json.dumps({"executable": str(managed / base.name)}),
    )

    result = deploy.cleanup_interpreter(tmp_path / "release", tmp_path / "root")

    assert result == {"executable": str(base), "sha256": digest(base)}


def test_cleanup_archive_contains_only_python_and_real_cleanup_entrypoint(tmp_path: Path) -> None:
    archive = tmp_path / "cleanup.pyz"
    deploy.cleanup_archive(archive)
    with zipfile.ZipFile(archive) as package:
        assert all(name.endswith(".py") for name in package.namelist())
        assert "from language_review.bootstrap import main" in package.read("__main__.py").decode()
        assert "def cleanup(" in package.read("language_review/distribution.py").decode()
        assert "Path(sys.argv[0]).unlink" in package.read("__main__.py").decode()


@pytest.mark.skipif(os.name != "nt", reason="Windows lease и внешняя очистка")
def test_cleanup_python_archive_removes_runtime_and_itself_without_path(tmp_path: Path) -> None:
    root = tmp_path / "application"
    installation(root, uninstalled=True)
    release = owned_release(root)
    helper = tmp_path / "cleanup.pyz"
    deploy.cleanup_archive(helper)
    process = subprocess.run(
        [sys._base_executable, "-I", str(helper), "--cleanup", "--root", str(root)],
        env={**os.environ, "PATH": ""},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["status"] == "uninstalled"
    assert not release.exists()
    assert not helper.exists()
    assert json.loads((root / "uninstall-result.json").read_text("utf-8"))["preserved"] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows lease и внешняя очистка")
def test_cleanup_preserves_unknown_modified_files_and_reports_busy_lock(tmp_path: Path) -> None:
    root = tmp_path / "application"
    installation(root, uninstalled=True)
    release = owned_release(root)
    modified = release / "owned.txt"
    modified.write_bytes(b"changed")
    unknown = release / "user.txt"
    unknown.write_bytes(b"user")
    lock = root / ".install-lock"
    lock.write_text("busy", encoding="ascii")
    result = deploy.cleanup(root)
    assert result == {"status": "cleanup_deferred", "reason": "install_lock_busy"}
    assert (release / ".run-leases/protocol").exists()
    lock.unlink()
    result = deploy.cleanup(root)
    assert result["preserved"]
    assert modified.read_bytes() == b"changed"
    assert unknown.read_bytes() == b"user"


@pytest.mark.skipif(os.name != "nt", reason="Windows удерживает EXE до закрытия handle")
def test_cleanup_retries_launcher_held_by_exclusive_windows_handle(tmp_path: Path) -> None:
    root = tmp_path / "application"
    installation(root, uninstalled=True)
    launcher = deploy.launcher_paths(root)[0]
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"synthetic owned executable")
    current = deploy.state(root)
    current["launchers"] = {launcher.name: digest(launcher)}
    deploy.atomic(root / "installation.json", deploy.json_bytes(current))
    ready = tmp_path / "launcher-held"
    child_code = (
        "import ctypes,sys,time; from pathlib import Path; "
        "k=ctypes.WinDLL('kernel32',use_last_error=True); "
        "k.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,"
        "ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]; "
        "k.CreateFileW.restype=ctypes.c_void_p; "
        "k.CloseHandle.argtypes=[ctypes.c_void_p]; "
        "h=k.CreateFileW(sys.argv[1],0x80000000,0,None,3,0x80,None); "
        "assert h != ctypes.c_void_p(-1).value,ctypes.get_last_error(); "
        "Path(sys.argv[2]).touch(); time.sleep(0.8); k.CloseHandle(ctypes.c_void_p(h))"
    )
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", child_code, str(launcher), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None, owner.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        result = deploy.cleanup(root)
        assert result["status"] == "uninstalled"
        assert result["preserved"] == []
        assert not launcher.exists()
        assert owner.wait(timeout=10) == 0, owner.communicate()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_cleanup_waits_for_owned_lease_process_before_deleting_runtime(tmp_path: Path) -> None:
    root = tmp_path / "application"
    installation(root, uninstalled=True)
    release = owned_release(root)
    lease = release / ".run-leases/owner.lock"
    ready = tmp_path / "ready"
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import msvcrt,sys,time; from pathlib import Path; "
            "f=Path(sys.argv[1]).open('x+b'); f.write(b'1'); f.flush(); f.seek(0); "
            "msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); Path(sys.argv[2]).touch(); "
            "time.sleep(0.8); f.close()",
            str(lease),
            str(ready),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None, owner.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert deploy._has_live_runtime_lease(release)
        result = deploy.cleanup(root, wait_pids=[owner.pid])
        assert result["preserved"] == []
        assert owner.poll() == 0
        assert not release.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Настоящий Windows launcher и venv")
def test_python_launcher_forwards_exact_arguments_and_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "application"
    installation(root)
    release = owned_release(root)
    venv.EnvBuilder(with_pip=False).create(release / "venv")
    package = release / "venv/Lib/site-packages/language_review"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json,sys\n"
        "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    arguments = ["пробел и кириллица", '"кавычки"', "", "--literal", "a&b|c"]
    helper = tmp_path / "launcher.pyz"
    deploy.cleanup_archive(helper)
    process = subprocess.run(
        [
            sys._base_executable,
            "-I",
            str(helper),
            "--launch",
            "review",
            "--root",
            str(root),
            "--",
            *arguments,
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": ""},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 7, process.stderr
    assert json.loads(process.stdout) == arguments
    assert not list((release / ".run-leases").glob("*.lock"))


def test_local_bootstrap_refuses_untrusted_bundle_before_uv_execution(
    tmp_path: Path, monkeypatch: Any
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "release.json").write_text("{}", encoding="utf-8")
    called: list[Path] = []
    monkeypatch.setattr(bootstrap, "obtain_uv", lambda directory, root: called.append(directory))
    from argparse import Namespace

    arguments = Namespace(
        root=tmp_path / "application",
        bundle_directory=bundle,
        trusted_manifest_sha256="0" * 64,
    )
    with pytest.raises(ReleaseError, match="доверенный SHA-256"):
        bootstrap.install(arguments)
    assert called == []
    assert not arguments.root.exists()


def test_local_bootstrap_rechecks_copied_manifest_before_uv_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "release.json"
    manifest.write_text("trusted", encoding="utf-8")
    (bundle / "artifact.whl").write_bytes(b"bundle")
    trusted_sha256 = digest(manifest)
    original_copy = bootstrap.shutil.copyfile
    called: list[Path] = []

    def mutate_manifest_before_copy(
        source: Path, destination: Path, *args: Any, **kwargs: Any
    ) -> str:
        if Path(source).name == "release.json":
            Path(source).write_text("changed", encoding="utf-8")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(bootstrap, "validate_bundle", lambda _directory: {})
    monkeypatch.setattr(bootstrap.shutil, "copyfile", mutate_manifest_before_copy)
    monkeypatch.setattr(bootstrap, "obtain_uv", lambda directory, root: called.append(directory))
    from argparse import Namespace

    arguments = Namespace(
        root=tmp_path / "application",
        bundle_directory=bundle,
        trusted_manifest_sha256=trusted_sha256,
    )
    with pytest.raises(ReleaseError, match="Копия release manifest"):
        bootstrap.install(arguments)
    assert called == []
    assert not arguments.root.exists()


@pytest.mark.parametrize("damage", ["missing", "duplicate", "url", "digest"])
def test_bootstrap_rejects_unverifiable_uv_before_archive_download(
    tmp_path: Path, monkeypatch: Any, damage: str
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    asset = {
        "name": "uv-x86_64-pc-windows-msvc.zip",
        "browser_download_url": "https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip",
        "digest": "sha256:" + "a" * 64,
    }
    assets = [asset]
    if damage == "missing":
        assets = []
    elif damage == "duplicate":
        assets *= 2
    elif damage == "url":
        asset["browser_download_url"] = "https://example.com/uv.zip"
    else:
        asset["digest"] = "sha256:" + "a" * 63
    calls: list[str] = []

    def download(url: str, target: Path, **kwargs: Any) -> None:
        calls.append(url)
        target.write_text(json.dumps({"assets": assets}), encoding="utf-8")

    monkeypatch.setattr(bootstrap, "download", download)
    with pytest.raises(ReleaseError, match="Нет проверяемой поставки uv"):
        bootstrap.obtain_uv(tmp_path, tmp_path / "application")
    assert calls == ["https://api.github.com/repos/astral-sh/uv/releases/tags/0.13.1"]
    assert not (tmp_path / "uv.zip").exists()
