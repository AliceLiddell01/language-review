from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from language_review import distribution as deploy
from language_review import setup_cli
from language_review.cli import main
from language_review.release_contract import (
    REPOSITORY,
    RUNTIME_ASSETS,
    ReleaseError,
    digest,
    package_version,
    validate_bundle,
    validate_manifest,
    version_key,
)
from language_review.release_transport import check_url
from language_review.setup_cli import configure_endpoints


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return make_bundle(tmp_path / "bundle", "0.0.0-rc.1")


def make_bundle(root: Path, version: str, previous: str = "none") -> Path:
    root.mkdir()
    pep = package_version(version)
    identity = {
        "version": version,
        "package_version": pep,
        "commit": "a" * 40,
        "tag": "v" + version,
    }
    with zipfile.ZipFile(root / f"language_review-{pep}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(
            f"language_review-{pep}.dist-info/METADATA", f"Name: language-review\nVersion: {pep}\n"
        )
        wheel.writestr("language_review/release_identity.json", json.dumps(identity))
        skill = Path("src/language_review/distribution_assets/SKILL.md").read_text("utf-8")
        wheel.writestr(
            "language_review/distribution_assets/SKILL.md", skill.replace("@VERSION@", pep)
        )
    with tarfile.open(root / f"language_review-{pep}.tar.gz", "w:gz") as sdist:
        content = f"Name: language-review\nVersion: {pep}\n".encode()
        entry = tarfile.TarInfo(f"language_review-{pep}/PKG-INFO")
        entry.size = len(content)
        sdist.addfile(entry, io.BytesIO(content))
        payload = json.dumps(identity).encode()
        entry = tarfile.TarInfo(f"language_review-{pep}/src/language_review/release_identity.json")
        entry.size = len(payload)
        sdist.addfile(entry, io.BytesIO(payload))
    for name in (
        "INSTALL.md",
        "install.ps1",
        "release-notes.md",
        "requirements-core.txt",
        "requirements-chatgpt.txt",
        "requirements-memory.txt",
        "requirements-all.txt",
        *RUNTIME_ASSETS,
    ):
        (root / name).write_text("Синтетический контрактный вход\n", encoding="utf-8")
    with zipfile.ZipFile(root / "wheelhouse.zip", "w") as wheels:
        wheels.writestr("synthetic-1.0-py3-none-any.whl", b"synthetic")
    manifest = {
        "schema": "language-review.release/1",
        "repository": REPOSITORY,
        **identity,
        "channel": "prerelease" if "-" in version else "stable",
        "previous_stable": previous,
        "python": "3.15",
        "skill_contract": 1,
        "lock_sha256": "b" * 64,
        "assets": {p.name: {"size": p.stat().st_size, "sha256": digest(p)} for p in root.iterdir()},
    }
    (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_release_bundle_identity_and_tamper(bundle: Path) -> None:
    data = validate_bundle(bundle)
    assert data["tag"] == "v0.0.0-rc.1"
    assert data["package_version"] == "0.0.0rc1"
    (bundle / "install.ps1").write_text("изменённый исполняемый asset", encoding="utf-8")
    with pytest.raises(ReleaseError, match="SHA-256"):
        validate_bundle(bundle)


@pytest.mark.parametrize(
    "version", ["1.2", "v1.2.3", "01.2.3", "1.2.3-rc.0", "1.2.3-dev.1", "1.2.3+local", "../1.2.3"]
)
def test_invalid_versions_rejected(version: str) -> None:
    with pytest.raises(ReleaseError):
        version_key(version)


def test_stable_and_prerelease_order() -> None:
    values = ["0.2.0-alpha.1", "0.2.0-alpha.2", "0.2.0-beta.1", "0.2.0-rc.1", "0.2.0", "1.0.0"]
    assert sorted(reversed(values), key=version_key) == values
    assert package_version("1.0.0-beta.12") == "1.0.0b12"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("repository", "other/repo"),
        ("commit", "main"),
        ("channel", "stable"),
        ("tag", "v0.1.0"),
        ("package_version", "0.1.0"),
        ("skill_contract", 2),
        ("python", "3.14"),
    ],
)
def test_manifest_rejects_identity_mismatch(bundle: Path, key: str, value: Any) -> None:
    data = json.loads((bundle / "release.json").read_text("utf-8"))
    data[key] = value
    with pytest.raises(ReleaseError):
        validate_manifest(data)


@pytest.mark.parametrize(
    ("previous", "valid"),
    [
        (None, False),
        ("none", True),
        ("v1.2.2", True),
        ("v1.x", False),
        ("v1.2.3-rc.1", False),
        ("v1.2.3", False),
        ("v2.0.0", False),
    ],
)
def test_manifest_validates_previous_stable(
    bundle: Path, previous: str | None, valid: bool
) -> None:
    data = json.loads((bundle / "release.json").read_text("utf-8"))
    data.update(
        {
            "version": "1.2.3",
            "package_version": "1.2.3",
            "tag": "v1.2.3",
            "channel": "stable",
        }
    )
    if previous is None:
        data.pop("previous_stable")
    else:
        data["previous_stable"] = previous
    if valid:
        validate_manifest(data)
    else:
        with pytest.raises(ReleaseError):
            validate_manifest(data)


@pytest.mark.parametrize(
    "name",
    [
        "../evil.ps1",
        "x/evil.ps1",
        "x\\evil.ps1",
        "C:evil.ps1",
        "a..b",
        "evil.ps1:stream",
        "CON.txt",
        "nul",
        "installer.",
    ],
)
def test_manifest_blocks_path_traversal(bundle: Path, name: str) -> None:
    data = json.loads((bundle / "release.json").read_text("utf-8"))
    data["assets"][name] = {"size": 1, "sha256": "a" * 64}
    with pytest.raises(ReleaseError, match="имя"):
        validate_manifest(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x",
        "https://github.com.evil.test/x",
        "https://user:secret@github.com/x",
        "https://github.com:8443/x",
        "file:///tmp/x",
        "https://evil.test/x",
    ],
)
def test_transport_blocks_unexpected_hosts(url: str) -> None:
    with pytest.raises(ReleaseError):
        check_url(url)


@pytest.mark.parametrize("name", ["../bad.whl", "directory/bad.whl", "bad.ps1", "C:bad.whl"])
def test_wheelhouse_zip_traversal_refused(tmp_path: Path, name: str) -> None:
    from language_review.release_contract import unpack_wheelhouse

    with zipfile.ZipFile(tmp_path / "wheelhouse.zip", "w") as archive:
        archive.writestr(name, b"unsafe")
    with pytest.raises(ReleaseError, match="Небезопасный"):
        unpack_wheelhouse(tmp_path, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()


def test_skill_conflict_and_transaction_rollback(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    root = tmp_path / "install"
    codex = deploy.skill_path("codex", home)
    claude = deploy.skill_path("claude", home)
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b"user skill")
    with pytest.raises(ReleaseError, match="Skill"):
        deploy.skill_writes({}, ["codex"], b"new", home)
    assert codex.read_bytes() == b"user skill"
    old = {"skills": {"codex": digest(codex)}}
    writes, hashes = deploy.skill_writes(old, ["codex", "claude"], b"new", home)
    assert hashes["claude"] == hashes["codex"]
    original = deploy.atomic

    def fail_claude(path: Path, data: bytes) -> None:
        if path == claude and data == b"new":
            raise OSError("синтетический отказ записи")
        original(path, data)

    monkeypatch.setattr(deploy, "atomic", fail_claude)
    with pytest.raises(OSError):
        deploy.transaction(root, writes)
    assert codex.read_bytes() == b"user skill"
    assert not claude.exists()
    assert not (root / ".transaction.json").exists()


def test_recover_interrupted_transaction(tmp_path: Path) -> None:
    import base64

    root = tmp_path / "install"
    root.mkdir()
    target = root / "pointer"
    target.write_bytes(b"new")
    import hashlib

    deploy.atomic(
        root / ".transaction.json",
        deploy.json_bytes(
            {
                "owner": deploy.OWNER,
                "files": [
                    {
                        "path": str(target),
                        "before": base64.b64encode(b"old").decode(),
                        "before_hash": hashlib.sha256(b"old").hexdigest(),
                        "after_hash": digest(target),
                    }
                ],
            }
        ),
    )
    deploy.recover(root)
    assert target.read_bytes() == b"old"


def test_setup_endpoints_preserves_consent_and_repositories(
    tmp_path: Path, monkeypatch: Any, isolated_appdata: Path
) -> None:
    repo = tmp_path / "foreign"
    repo.mkdir()
    source = repo / "source.md"
    source.write_bytes(b"unchanged\r\n")
    monkeypatch.chdir(repo)
    configure_endpoints({"ollama": "http://127.0.0.1:11434"})
    configure_endpoints({"languagetool": "http://localhost:8081"})
    from language_review.config import load_config, user_config_path

    config = load_config()
    text = user_config_path().read_text("utf-8")
    assert "11434" in text and "8081" in text
    assert "allow_cloud" not in text and "model" not in text
    assert not config.check.llm_chatgpt_allow_cloud
    assert source.read_bytes() == b"unchanged\r\n"
    assert not (repo / ".language-review.yaml").exists()
    user_config_path().write_text("# пользовательский файл\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="вручную"):
        configure_endpoints({"ollama": "http://127.0.0.1:11434"})
    assert user_config_path().read_text("utf-8") == "# пользовательский файл\n"


def test_setup_refuses_remote_without_mutation(isolated_appdata: Path) -> None:
    from language_review.config import user_config_path

    before = user_config_path().read_bytes() if user_config_path().exists() else None
    with pytest.raises(ReleaseError, match="локальные"):
        configure_endpoints({"ollama": "https://user:password@remote.test"})
    assert (user_config_path().read_bytes() if user_config_path().exists() else None) == before


@pytest.mark.parametrize(
    ("skills", "answers", "expected", "prompt_suffix"),
    [
        ({"codex": "a" * 64}, ["", "", "", "", ""], ["codex"], "[Д/н] "),
        ({"codex": "a" * 64}, ["н", "", "", "", ""], [], "[Д/н] "),
        ({}, ["", "", "", "", ""], [], "[д/Н] "),
    ],
)
def test_interactive_setup_preserves_unspecified_skills(
    tmp_path: Path,
    monkeypatch: Any,
    skills: dict[str, str],
    answers: list[str],
    expected: list[str],
    prompt_suffix: str,
) -> None:
    from language_review import doctor

    current = {"extras": "core", "skills": skills}
    selected: list[str] = []
    prompts: list[str] = []
    answer_iterator = iter(answers)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answer_iterator)

    monkeypatch.setattr(setup_cli.deployment, "managed_root", lambda: tmp_path)
    monkeypatch.setattr(setup_cli.deployment, "state", lambda _root: current)
    monkeypatch.setattr(
        setup_cli.deployment, "setup_skills", lambda _root, agents: selected.extend(agents)
    )
    monkeypatch.setattr(setup_cli, "configure_endpoints", lambda _values: None)
    monkeypatch.setattr(setup_cli, "load_config", lambda: object())
    monkeypatch.setattr(doctor, "run_doctor", lambda *_args: {})
    monkeypatch.setattr(doctor, "doctor_human", lambda _report: "")
    monkeypatch.setattr("builtins.input", fake_input)

    assert main(["setup"]) == 0
    assert selected == expected
    assert prompts[0].endswith(prompt_suffix)


def test_installation_update_uses_inventory_verified_private_uv(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    root = tmp_path / "application"
    active = "a" * 32
    release = root / "releases" / active
    tools = release / "tools"
    tools.mkdir(parents=True)
    private_uv = tools / "uv.exe"
    private_uv.write_bytes(b"verified uv")
    (release / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {str(Path("tools") / "uv.exe"): digest(private_uv)},
            }
        ),
        encoding="utf-8",
    )
    current = {
        "active": active,
        "uv": str(private_uv),
        "extras": "core",
        "home": str(tmp_path / "home"),
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(setup_cli.deployment, "managed_root", lambda: root)
    monkeypatch.setattr(setup_cli.deployment, "state", lambda _root: current)
    monkeypatch.setattr(
        "language_review.release_transport.fetch_bundle", lambda *_args, **_kwargs: None
    )

    def install(*_args: Any, **kwargs: Any) -> dict[str, str]:
        captured["uv"] = kwargs["uv"]
        return {"version": "1.2.3"}

    monkeypatch.setattr(setup_cli.deployment, "install", install)
    arguments = SimpleNamespace(
        command="installation",
        installation_command="update",
        tag=None,
        prerelease=False,
        extras=None,
        allow_downgrade=False,
    )

    assert setup_cli.run(arguments) == 0
    assert captured["uv"] == str(private_uv)
    assert '"status": "updated"' in capsys.readouterr().out


def test_installation_update_rejects_unverified_private_uv(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    root = tmp_path / "application"
    active = "b" * 32
    current = {
        "active": active,
        "uv": "C:/unrelated/uv.exe",
        "extras": "core",
        "home": str(tmp_path / "home"),
    }
    fetched: list[bool] = []
    monkeypatch.setattr(setup_cli.deployment, "managed_root", lambda: root)
    monkeypatch.setattr(setup_cli.deployment, "state", lambda _root: current)
    monkeypatch.setattr(
        "language_review.release_transport.fetch_bundle",
        lambda *_args, **_kwargs: fetched.append(True),
    )
    arguments = SimpleNamespace(
        command="installation",
        installation_command="update",
        tag=None,
        prerelease=False,
        extras=None,
        allow_downgrade=False,
    )

    assert setup_cli.run(arguments) == 2
    assert not fetched
    assert "не соответствует active runtime" in capsys.readouterr().err


def test_prune_stale_releases_removes_only_owned_inactive_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "application"
    stale_id = "c" * 32
    active_id = "d" * 32
    previous_id = "e" * 32
    stale = root / "releases" / stale_id
    leases = stale / ".run-leases"
    leases.mkdir(parents=True)
    protocol = leases / "protocol"
    protocol.write_text("1\n", encoding="ascii")
    module = stale / "bundle" / "module.py"
    module.parent.mkdir()
    module.write_text("owned\n", encoding="utf-8")
    (stale / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {
                    str(Path(".run-leases") / "protocol"): digest(protocol),
                    str(Path("bundle") / "module.py"): digest(module),
                },
            }
        ),
        encoding="utf-8",
    )
    for identifier in (active_id, previous_id):
        (root / "releases" / identifier).mkdir(parents=True)

    removed = deploy.prune_stale_releases(root, active=active_id, previous=previous_id)

    assert removed == [stale_id]
    assert not stale.exists()
    assert (root / "releases" / active_id).is_dir()
    assert (root / "releases" / previous_id).is_dir()


def test_prune_stale_releases_preserves_modified_and_legacy_generations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "application"
    modified_id = "f" * 32
    legacy_id = "0" * 32
    modified = root / "releases" / modified_id
    leases = modified / ".run-leases"
    leases.mkdir(parents=True)
    protocol = leases / "protocol"
    protocol.write_text("1\n", encoding="ascii")
    module = modified / "bundle" / "module.py"
    module.parent.mkdir()
    module.write_text("original\n", encoding="utf-8")
    (modified / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {
                    str(Path(".run-leases") / "protocol"): digest(protocol),
                    str(Path("bundle") / "module.py"): digest(module),
                },
            }
        ),
        encoding="utf-8",
    )
    module.write_text("user change\n", encoding="utf-8")
    legacy = root / "releases" / legacy_id
    legacy.mkdir()
    (legacy / "unknown.txt").write_text("preserve", encoding="utf-8")

    removed = deploy.prune_stale_releases(root, active="1" * 32, previous="2" * 32)

    assert not removed
    assert module.read_text("utf-8") == "user change\n"
    assert (legacy / "unknown.txt").read_text("utf-8") == "preserve"


@pytest.mark.skipif(os.name != "nt", reason="Lease использует Windows byte-range lock")
def test_prune_stale_releases_preserves_live_lease(
    tmp_path: Path,
) -> None:
    import msvcrt

    root = tmp_path / "application"
    identifier = "9" * 32
    release = root / "releases" / identifier
    leases = release / ".run-leases"
    leases.mkdir(parents=True)
    protocol = leases / "protocol"
    protocol.write_text("1\n", encoding="ascii")
    lease = leases / "running.lock"
    lease.write_bytes(b"1")
    module = release / "module.py"
    module.write_text("owned", encoding="utf-8")
    (release / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {
                    str(Path(".run-leases") / "protocol"): digest(protocol),
                    "module.py": digest(module),
                },
            }
        ),
        encoding="utf-8",
    )
    with lease.open("r+b") as stream:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        assert not deploy.prune_stale_releases(root, active="1" * 32, previous="2" * 32)
        assert release.is_dir()
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    assert deploy.prune_stale_releases(root, active="1" * 32, previous="2" * 32) == [identifier]


@pytest.mark.skipif(os.name != "nt", reason="Контракт PowerShell Windows")
def test_uninstall_cleanup_reports_deferred_when_lock_is_busy(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 не найден.")
    root = tmp_path / "application"
    root.mkdir()
    lock = root / ".install-lock"
    lock.write_text("another operation", encoding="utf-8")
    script = Path("src/language_review/distribution_assets/uninstall-cleanup.ps1").resolve()

    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), "-Root", str(root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    outcome = json.loads((root / "uninstall-result.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "cleanup_deferred"
    assert outcome["reason"] == "install_lock_busy"
    assert lock.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Контракт PowerShell Windows")
def test_uninstall_cleanup_retries_removing_locked_owned_file(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 не найден.")
    root = tmp_path / "application"
    release = root / "releases" / ("a" * 32)
    runtime = release / "venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"synthetic venv redirector")
    (root / "installation.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "root": str(root),
                "uninstalled": True,
            }
        ),
        encoding="utf-8",
    )
    (release / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {"venv/Scripts/python.exe": digest(runtime)},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "lock-ready"
    holder = tmp_path / "hold-file.ps1"
    holder.write_text(
        "$handle = [IO.File]::Open($args[0], 'Open', 'Read', 'Read')\n"
        "[IO.File]::WriteAllText($args[1], 'ready')\n"
        "Start-Sleep -Milliseconds 700\n"
        "$handle.Dispose()\n",
        encoding="utf-8",
    )
    lock_process = subprocess.Popen(
        [pwsh, "-NoProfile", "-File", str(holder), str(runtime), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            if lock_process.poll() is not None or time.monotonic() > deadline:
                pytest.fail("Не удалось захватить синтетический файл runtime.")
            time.sleep(0.02)

        script = tmp_path / "uninstall-cleanup.ps1"
        script.write_bytes(
            Path("src/language_review/distribution_assets/uninstall-cleanup.ps1")
            .resolve()
            .read_bytes()
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(script), "-Root", str(root)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads((root / "uninstall-result.json").read_text(encoding="utf-8"))
        assert outcome["status"] == "uninstalled"
        assert "venv/Scripts/python.exe" not in outcome["preserved"]
        assert not runtime.exists()
    finally:
        if lock_process.poll() is None:
            lock_process.terminate()
        lock_process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Lease использует exclusive Windows sharing")
def test_uninstall_cleanup_preserves_runtime_with_live_cli_lease(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 не найден.")
    root = tmp_path / "application"
    identifier = "b" * 32
    release = root / "releases" / identifier
    leases = release / ".run-leases"
    leases.mkdir(parents=True)
    protocol = leases / "protocol"
    protocol.write_text("1\n", encoding="ascii")
    lease = leases / "running.lock"
    lease.write_bytes(b"1")
    runtime = release / "venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"synthetic venv redirector")
    (root / "installation.json").write_text(
        json.dumps({"owner": deploy.OWNER, "root": str(root), "uninstalled": True}),
        encoding="utf-8",
    )
    (release / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {
                    str(Path(".run-leases") / "protocol"): digest(protocol),
                    "venv/Scripts/python.exe": digest(runtime),
                },
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "lease-ready"
    holder = tmp_path / "hold-lease.ps1"
    holder.write_text(
        "$handle = [IO.File]::Open($args[0], 'Open', 'ReadWrite', 'None')\n"
        "[IO.File]::WriteAllText($args[1], 'ready')\n"
        "Start-Sleep -Milliseconds 1200\n"
        "$handle.Dispose()\n",
        encoding="utf-8",
    )
    lock_process = subprocess.Popen(
        [pwsh, "-NoProfile", "-File", str(holder), str(lease), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            if lock_process.poll() is not None or time.monotonic() > deadline:
                pytest.fail("Не удалось захватить runtime lease.")
            time.sleep(0.02)
        script = tmp_path / "uninstall-cleanup.ps1"
        script.write_bytes(
            Path("src/language_review/distribution_assets/uninstall-cleanup.ps1")
            .resolve()
            .read_bytes()
        )

        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(script), "-Root", str(root)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        outcome = json.loads((root / "uninstall-result.json").read_text(encoding="utf-8"))
        assert outcome["status"] == "uninstalled"
        assert outcome["preserved"] == ["Runtime занят работающим CLI: " + identifier]
        assert runtime.read_bytes() == b"synthetic venv redirector"
    finally:
        if lock_process.poll() is None:
            lock_process.terminate()
        lock_process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Контракт PowerShell Windows")
def test_uninstall_cleanup_waits_for_launcher_before_checking_lease(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 не найден.")
    root = tmp_path / "application"
    identifier = "c" * 32
    release = root / "releases" / identifier
    leases = release / ".run-leases"
    leases.mkdir(parents=True)
    protocol = leases / "protocol"
    protocol.write_text("1\n", encoding="ascii")
    lease = leases / "running.lock"
    lease.write_bytes(b"1")
    runtime = release / "venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"synthetic venv redirector")
    (root / "installation.json").write_text(
        json.dumps({"owner": deploy.OWNER, "root": str(root), "uninstalled": True}),
        encoding="utf-8",
    )
    (release / "owned-files.json").write_text(
        json.dumps(
            {
                "owner": deploy.OWNER,
                "files": {
                    str(Path(".run-leases") / "protocol"): digest(protocol),
                    "venv/Scripts/python.exe": digest(runtime),
                },
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "launcher-ready"
    holder = tmp_path / "hold-launcher-lease.ps1"
    holder.write_text(
        "$handle = [IO.File]::Open($args[0], 'Open', 'ReadWrite', 'None')\n"
        "[IO.File]::WriteAllText($args[1], 'ready')\n"
        "Start-Sleep -Milliseconds 700\n"
        "$handle.Dispose()\n",
        encoding="utf-8",
    )
    launcher = subprocess.Popen(
        [pwsh, "-NoProfile", "-File", str(holder), str(lease), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            if launcher.poll() is not None or time.monotonic() > deadline:
                pytest.fail("Не удалось захватить runtime lease процессом launcher.")
            time.sleep(0.02)
        script = tmp_path / "uninstall-cleanup.ps1"
        script.write_bytes(
            Path("src/language_review/distribution_assets/uninstall-cleanup.ps1")
            .resolve()
            .read_bytes()
        )

        result = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-File",
                str(script),
                "-Root",
                str(root),
                "-WaitForLauncherPid",
                str(launcher.pid),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        outcome = json.loads((root / "uninstall-result.json").read_text(encoding="utf-8"))
        assert outcome["status"] == "uninstalled"
        assert outcome["preserved"] == []
        assert not runtime.exists()
    finally:
        if launcher.poll() is None:
            launcher.terminate()
        launcher.wait(timeout=5)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 не найден")
def test_generated_launcher_parses_in_powershell(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    launcher = tmp_path / "language-review-launch.ps1"
    launcher_bytes = deploy.launcher_files(tmp_path)[tmp_path / "bin/language-review-launch.ps1"]
    assert b"$env:LANGUAGE_REVIEW_LAUNCHER_PID = [string]$PID" in launcher_bytes
    launcher.write_bytes(launcher_bytes)
    parser = tmp_path / "parse-powershell.ps1"
    parser.write_text(
        "param([string]$Path)\n"
        "$tokens = $null\n$errors = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile(\n"
        "    $Path,\n    [ref]$tokens,\n    [ref]$errors\n)\n"
        "if ($errors) { $errors | Out-String | Write-Error; exit 1 }\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(parser), str(launcher)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_generated_batch_launchers_exit_on_the_invocation_line(tmp_path: Path) -> None:
    launchers = deploy.launcher_files(tmp_path)
    for name in ("language-review.cmd", "language-review-eval.cmd"):
        invocation = launchers[tmp_path / "bin" / name].splitlines()[1]
        assert invocation.endswith(b" %* & exit /b")


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-планировщика")
def test_schedule_cleanup_waits_for_venv_parent_process(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(deploy.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(deploy.os, "getpid", lambda: 1234)
    monkeypatch.setattr(deploy.os, "getppid", lambda: 5678)
    monkeypatch.setenv("LANGUAGE_REVIEW_LAUNCHER_PID", "8765")
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(deploy.subprocess, "Popen", fake_popen)

    deploy.schedule_cleanup(tmp_path / "application")

    args = captured["args"]
    assert args[args.index("-WaitForPid") + 1] == "1234"
    assert args[args.index("-WaitForParentPid") + 1] == "5678"
    assert args[args.index("-WaitForLauncherPid") + 1] == "8765"
    assert captured["kwargs"]["creationflags"] == subprocess.CREATE_NO_WINDOW


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_idempotent_install_applies_new_path_request(
    bundle: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "application"
    home = tmp_path / "home"
    active = "a" * 32
    (root / "releases" / active).mkdir(parents=True)
    data = validate_bundle(bundle)
    initial = {
        "owner": deploy.OWNER,
        "root": str(root),
        "active": active,
        "version": data["version"],
        "package_version": data["package_version"],
        "commit": data["commit"],
        "manifest_sha256": digest(bundle / "release.json"),
        "extras": "core",
        "skills": {},
        "home": str(home),
        "path_added": False,
        "path_requested": False,
        "launchers": {},
    }
    deploy.atomic(root / "installation.json", deploy.json_bytes(initial))
    monkeypatch.setattr(deploy, "probe", lambda *_args: None)
    monkeypatch.setattr(deploy, "path_conflict", lambda *_args: None)
    path_changes: list[bool] = []

    def change_path(_bin_path: Path, *, add: bool) -> bool:
        path_changes.append(add)
        return add

    monkeypatch.setattr(deploy, "change_path", change_path)

    current = deploy.install(bundle, root, uv="unused", home=home, add_path=True)

    assert path_changes == [True]
    assert current["path_added"] is True
    assert current["path_requested"] is True
    assert deploy.state(root)["path_added"] is True


def test_distribution_cli_leaves_unspecified_install_preferences_unset(
    bundle: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(deploy, "default_root", lambda: tmp_path / "application")

    def capture_install(*_args: Any, **kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"version": "1.2.3"}

    monkeypatch.setattr(deploy, "install", capture_install)
    monkeypatch.setattr(
        sys,
        "argv",
        ["language_review.distribution", "--bundle", str(bundle), "--uv", "uv.exe"],
    )
    assert deploy.main() == 0
    capsys.readouterr()

    assert captured["extras"] is None
    assert captured["add_path"] is None


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_install_compiles_bytecode_before_recording_inventory(
    bundle: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "application"
    fake_uv = tmp_path / "uv.exe"
    fake_uv.write_bytes(b"synthetic")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        deploy,
        "execute",
        lambda args, **_kwargs: commands.append(args) or "",
    )
    monkeypatch.setattr(deploy, "probe", lambda *_args: None)
    monkeypatch.setattr(deploy, "path_conflict", lambda *_args: None)

    deploy.install(bundle, root, uv=str(fake_uv), home=tmp_path / "home", add_path=False)

    installs = [args for args in commands if args[1:3] == ["pip", "install"]]
    assert len(installs) == 2
    assert all("--compile-bytecode" in args for args in installs)
    assert "--require-hashes" in installs[0]
    assert "--no-deps" in installs[1]


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_prune_failure_does_not_remove_committed_runtime(
    bundle: Path, tmp_path: Path, monkeypatch: Any, failure: type[BaseException]
) -> None:
    root = tmp_path / "application"
    fake_uv = tmp_path / "uv.exe"
    fake_uv.write_bytes(b"synthetic")
    monkeypatch.setattr(deploy, "execute", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(deploy, "probe", lambda *_args: None)
    monkeypatch.setattr(deploy, "path_conflict", lambda *_args: None)

    def fail(*_args: Any, **_kwargs: Any) -> list[str]:
        raise failure("synthetic cleanup failure")

    monkeypatch.setattr(deploy, "prune_stale_releases", fail)
    if failure is OSError:
        current = deploy.install(bundle, root, uv=str(fake_uv), add_path=False)
    else:
        with pytest.raises(KeyboardInterrupt, match="synthetic cleanup failure"):
            deploy.install(bundle, root, uv=str(fake_uv), add_path=False)
        current = deploy.state(root)

    assert deploy.state(root)["active"] == current["active"]
    assert (root / "releases" / current["active"]).is_dir()


def release_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_script", "scripts/release.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def distribution_verifier_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "distribution_verifier", "scripts/verify-distribution.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_distribution_conditions_remain_active_under_optimization() -> None:
    script = Path("scripts/verify-distribution.py").resolve()
    code = "\n".join(
        (
            "import ast",
            "import runpy",
            "import sys",
            "path = sys.argv[1]",
            "tree = ast.parse(open(path, encoding='utf-8').read())",
            "if any(isinstance(node, ast.Assert) for node in ast.walk(tree)):",
            "    raise SystemExit('assert statement remains')",
            "runpy.run_path(path, run_name='verify_distribution_test')"
            "['require'](False, 'synthetic optimization check')",
        )
    )
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    result = subprocess.run(
        [sys.executable, "-O", "-c", code, str(script)],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert "FAIL: synthetic optimization check" in result.stderr
    assert "assert statement remains" not in result.stderr


def test_personal_skill_probe_removes_every_created_profile_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    sandbox = tmp_path / "sandbox"
    source = sandbox / ".agents/skills/language-review/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic acceptance skill")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: profile))
    module = distribution_verifier_module()

    with module.personal_skill_probe(sandbox) as installed:
        assert installed.read_bytes() == source.read_bytes()
        assert (profile / ".agents/skills/language-review").is_dir()

    assert not (profile / ".agents").exists()


def test_personal_skill_probe_preserves_preexisting_profile_directories(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profile = tmp_path / "profile"
    existing = profile / ".agents"
    existing.mkdir(parents=True)
    sandbox = tmp_path / "sandbox"
    source = sandbox / ".agents/skills/language-review/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic acceptance skill")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: profile))
    module = distribution_verifier_module()

    with module.personal_skill_probe(sandbox):
        pass

    assert existing.is_dir()
    assert not (existing / "skills").exists()


@pytest.mark.parametrize(
    ("codex", "upgrade"),
    [("tools/codex.exe", "dist/upgrade"), (None, None)],
)
def test_distribution_verifier_resolves_optional_paths(
    tmp_path: Path, monkeypatch: Any, codex: str | None, upgrade: str | None
) -> None:
    module = distribution_verifier_module()
    captured: dict[str, Any] = {}

    def verify(
        candidate: Path, codex_executable: Path | None, upgrade_candidate: Path | None
    ) -> dict[str, str]:
        captured.update(
            candidate=candidate,
            codex=codex_executable,
            upgrade=upgrade_candidate,
        )
        return {"status": "PASS"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "verify", verify)
    arguments = [
        "verify-distribution.py",
        "--candidate",
        "candidate",
        "--output",
        str(tmp_path / "acceptance.json"),
    ]
    if codex is not None and upgrade is not None:
        arguments.extend(["--codex-executable", codex, "--upgrade-candidate", upgrade])
    monkeypatch.setattr(sys, "argv", arguments)

    module.main()

    assert captured["candidate"] == (tmp_path / "candidate").absolute()
    assert captured["codex"] == ((tmp_path / codex).absolute() if codex else None)
    assert captured["upgrade"] == ((tmp_path / upgrade).absolute() if upgrade else None)
    assert json.loads((tmp_path / "acceptance.json").read_text("utf-8")) == {"status": "PASS"}


def test_distribution_verifier_requires_a_strictly_newer_upgrade() -> None:
    module = distribution_verifier_module()
    module.require_upgrade_version({"version": "1.2.3"}, {"version": "1.2.4"})

    for version in ("1.2.3", "1.2.2"):
        with pytest.raises(RuntimeError, match="более новую версию"):
            module.require_upgrade_version({"version": "1.2.3"}, {"version": version})


def test_distribution_verifier_isolates_checkout_and_bootstrap_paths(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = distribution_verifier_module()
    checkout_scripts = tmp_path / "checkout" / "Scripts"
    tools = tmp_path / "shared-tools"
    other = tmp_path / "other"
    for directory in (checkout_scripts, tools, other):
        directory.mkdir(parents=True)
    uv = tools / "uv.exe"
    uv.write_bytes(b"synthetic uv")
    monkeypatch.setattr(module.sys, "prefix", str(checkout_scripts.parent))
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name, path=None: str(uv) if name == "uv" else None,
    )
    source_path = os.pathsep.join(map(str, (checkout_scripts, tools, other)))
    runtime, bootstrap = module.isolated_environments({"PATH": source_path, "MARKER": "kept"})

    assert runtime["MARKER"] == bootstrap["MARKER"] == "kept"
    assert str(checkout_scripts) not in runtime["PATH"]
    assert str(checkout_scripts) not in bootstrap["PATH"]
    assert str(tools) in runtime["PATH"] and str(tools) not in bootstrap["PATH"]
    assert str(other) in runtime["PATH"] and str(other) in bootstrap["PATH"]


def test_distribution_verifier_retargets_bootstrap_bundle_and_digest(tmp_path: Path) -> None:
    module = distribution_verifier_module()
    bundle = tmp_path / "candidate"
    bundle.mkdir()
    manifest = bundle / "release.json"
    manifest.write_text('{"version":"1.2.3"}', encoding="utf-8")
    bootstrap = [
        "pwsh.exe",
        "-File",
        "install.ps1",
        "-BundleDirectory",
        "old",
        "-TrustedManifestSha256",
        "old-digest",
    ]

    result = module.bootstrap_for_bundle(bootstrap, bundle)

    assert result[result.index("-BundleDirectory") + 1] == str(bundle)
    assert result[result.index("-TrustedManifestSha256") + 1] == digest(manifest)
    assert bootstrap[4:7] == ["old", "-TrustedManifestSha256", "old-digest"]


@pytest.mark.skipif(os.name != "nt", reason="Исполняемый Windows bootstrap")
@pytest.mark.parametrize("prerelease", [False, True])
@pytest.mark.parametrize("channel", ["stable", "prerelease"])
def test_bootstrap_checks_release_channel_consistency(
    tmp_path: Path, monkeypatch: Any, prerelease: bool, channel: str
) -> None:
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    uv_bin = tmp_path / "uv-bin"
    uv_bin.mkdir()
    (uv_bin / "uv.cmd").write_text("@echo off\r\necho uv 0.13.1\r\n", encoding="utf-8")
    windows_root = Path(os.environ["SystemRoot"])
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(uv_bin), str(windows_root / "System32"), str(windows_root)]),
    )
    # Подменяется только сеть. AST выделяет downloader; guard и весь основной
    # установщик выполняются PowerShell без изменений.
    (tmp_path / "release.json").write_text(
        json.dumps(
            {
                "tag_name": "v1.0.0",
                "draft": False,
                "published_at": "date",
                "prerelease": prerelease,
                "assets": [
                    {
                        "name": "release.json",
                        "digest": "sha256:" + "b" * 64,
                        "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v1.0.0/release.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "repository": REPOSITORY,
                "tag": "v1.0.0",
                "commit": "a" * 40,
                "channel": channel,
            }
        ),
        encoding="utf-8",
    )
    wrapper = tmp_path / "bootstrap-boundary.ps1"
    wrapper.write_text(
        """param([string]$Script, [string]$Fixtures)
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$tokens, [ref]$errors)
if ($errors) { throw 'Не удалось разобрать установщик.' }
$fn = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Get-VerifiedDownload'}, $true)
$replacement = @'
function Get-VerifiedDownload([string]$Url, [string]$Destination, [string]$Hash, [long]$Limit) {
    if ($Url -like '*/git/ref/tags/*') { throw 'GUARD_PASSED_BOUNDARY' }
    $name = if ($Url -like '*/releases/tags/*') { 'release.json' } else { 'manifest.json' }
    Copy-Item -LiteralPath (Join-Path $env:LR_TEST_FIXTURES $name) -Destination $Destination
}
'@
$env:LR_TEST_FIXTURES = $Fixtures
$text = [IO.File]::ReadAllText($Script)
$text = $text.Remove($fn.Extent.StartOffset, $fn.Extent.EndOffset - $fn.Extent.StartOffset)
$text = $text.Insert($fn.Extent.StartOffset, $replacement)
$root = Join-Path $Fixtures 'unused-root'
& ([scriptblock]::Create($text)) -Tag v1.0.0 -Root $root -NonInteractive
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(wrapper),
            str(Path("scripts/install.ps1").absolute()),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode != 0
    if prerelease == (channel == "prerelease"):
        assert "GUARD_PASSED_BOUNDARY" in result.stderr
    else:
        assert "Manifest не соответствует GitHub release" in result.stderr
        assert "GUARD_PASSED_BOUNDARY" not in result.stderr
    assert not (tmp_path / "unused-root").exists()


def test_distribution_verifier_reports_missing_vale_as_not_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = distribution_verifier_module()
    calls: list[Any] = []
    monkeypatch.setattr(module, "command", lambda *args: calls.append(args))
    checks: dict[str, Any] = {}
    # Настоящий поиск программы в пустом PATH, без подмены результата which.
    module.verify_vale(["unused"], {"PATH": str(tmp_path / "empty")}, tmp_path, tmp_path, checks)
    assert checks == {
        "real_vale_findings_clean_stale_hash": {
            "status": "NOT_RUN",
            "reason": "vale_unavailable",
        }
    }
    assert not calls


def test_release_command_error_includes_bounded_stderr(monkeypatch: Any) -> None:
    module = release_module()
    stderr = "EARLY_DIAGNOSTIC " + ("x" * 2500) + " FINAL_DIAGNOSTIC"
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr=stderr),
    )

    with pytest.raises(ReleaseError) as failure:
        module.run("gh", "release", "create")

    message = str(failure.value)
    assert "Не выполнена команда: gh release create" in message
    assert "FINAL_DIAGNOSTIC" in message
    assert "EARLY_DIAGNOSTIC" not in message


def test_publication_requires_external_authorization(bundle: Path, monkeypatch: Any) -> None:
    module = release_module()
    import argparse

    monkeypatch.delenv("LR_RELEASE_AUTHORIZATION", raising=False)
    called = []
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: called.append(args))
    with pytest.raises(ReleaseError, match="разрешения"):
        module.publish(argparse.Namespace(candidate=bundle, evidence=bundle / "absent.json"))
    assert not called


def test_release_history_guards_previous_stable_and_repeated_versions() -> None:
    module = release_module()
    history = [
        {"tag_name": "v0.1.0", "published_at": "date", "prerelease": False},
        {"tag_name": "v0.2.0-rc.1", "published_at": "date", "prerelease": True},
    ]
    module.check_target("0.2.0", "a" * 40, "v0.1.0", history)
    module.check_target("0.1.1", "a" * 40, "v0.1.0", history)
    for version, commit, previous in (
        ("0.2.0-rc.1", "a" * 40, "v0.1.0"),
        ("0.0.9", "a" * 40, "v0.1.0"),
        ("0.2.0", "main", "v0.1.0"),
        ("0.2.0", "a" * 40, "v0.2.0-rc.1"),
    ):
        with pytest.raises(ReleaseError):
            module.check_target(version, commit, previous, history)


@pytest.mark.parametrize(
    ("blocker", "expected"),
    [
        ("tag", "Тег уже существует; повторная публикация запрещена."),
        ("ci", "Обязательный CI не завершён успешно для target SHA: ci.yml"),
        ("evidence", "Нет приёмки Windows именно этого комплекта."),
        (
            "notes",
            "В описании выпуска нужны версия, SHA, предыдущий выпуск и заполненные разделы.",
        ),
    ],
)
def test_publication_preconditions_never_reach_write(
    bundle: Path, monkeypatch: Any, blocker: str, expected: str
) -> None:
    import argparse

    module = release_module()
    if blocker != "notes":
        notes = (
            "0.0.0-rc.1 none "
            + "a" * 40
            + "\n"
            + "\n".join(
                title + "\nПроверенное содержимое.\n"
                for title in (
                    "## Изменения",
                    "## Совместимость",
                    "## Миграции и откат",
                    "## Ограничения",
                )
            )
        )
        (bundle / "release-notes.md").write_text(notes, encoding="utf-8")
        manifest = json.loads((bundle / "release.json").read_text("utf-8"))
        manifest["assets"]["release-notes.md"] = {
            "size": (bundle / "release-notes.md").stat().st_size,
            "sha256": digest(bundle / "release-notes.md"),
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("LR_RELEASE_AUTHORIZATION", "publish v0.0.0-rc.1 " + "a" * 40)
    monkeypatch.setenv("LR_NOTES_SHA256", digest(bundle / "release-notes.md"))
    monkeypatch.setattr(module, "release_history", lambda: [])
    calls = []

    def run(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        if args[:2] == ("git", "ls-remote"):
            return "existing" if blocker == "tag" else ""
        raise AssertionError("Write-команда не должна достигаться")

    monkeypatch.setattr(module, "run", run)
    checks = (
        []
        if blocker == "ci"
        else [
            {
                "id": 1,
                "path": ".github/workflows/" + name,
                "head_sha": "a" * 40,
                "conclusion": "success",
            }
            for name in ("ci.yml", "codeql.yml")
        ]
    )
    monkeypatch.setattr(module, "github", lambda path: {"workflow_runs": checks})
    evidence = bundle.parent / "acceptance.json"
    if blocker == "evidence":
        from language_review import release_acceptance as acceptance

        manifest = json.loads((bundle / "release.json").read_text("utf-8"))
        completed_checks = {
            name: {"status": "PASS"} for name in acceptance.REQUIRED | acceptance.VERSION_CHECKS
        }
        report = acceptance.report(manifest, digest(bundle / "release.json"), completed_checks)
        report["commit"] = "b" * 40
    else:
        report = {
            "status": "PASS",
            "commit": "a" * 40,
            "manifest_sha256": digest(bundle / "release.json"),
        }
    evidence.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ReleaseError) as error:
        module.publish(argparse.Namespace(candidate=bundle, evidence=evidence))
    assert str(error.value) == expected
    assert not any(args[:2] == ("gh", "release") for args in calls)


def test_asset_download_hash_failure_and_partial_cleanup(tmp_path: Path, monkeypatch: Any) -> None:

    from language_review import release_transport as transport

    class Response(io.BytesIO):
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            return Response(b"independently received invalid content")

    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *args: Opener())
    target = tmp_path / "asset"
    with pytest.raises(ReleaseError, match="SHA-256"):
        transport.download("https://github.com/file", target, "a" * 64)
    assert not target.exists() and not target.with_name("asset.partial").exists()
    with pytest.raises(ReleaseError, match="размер"):
        transport.download("https://github.com/file", target, limit=1)
    assert not target.exists() and not target.with_name("asset.partial").exists()


@pytest.mark.parametrize(
    ("url", "expected_token"),
    [
        ("https://api.github.com/repos/AliceLiddell01/language-review/releases/latest", True),
        (
            "https://github.com/AliceLiddell01/language-review/releases/download/v1.0.0/file.zip",
            False,
        ),
    ],
)
def test_download_sends_token_only_to_github_api(
    tmp_path: Path, monkeypatch: Any, url: str, expected_token: bool
) -> None:
    from language_review import release_transport as transport

    class Response(io.BytesIO):
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

    requests: list[Any] = []

    class Opener:
        def open(self, request: Any, **_kwargs: Any) -> Response:
            requests.append(request)
            return Response(b"metadata")

    monkeypatch.setenv("GH_TOKEN", "synthetic-test-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *_args: Opener())
    transport.download(url, tmp_path / "download")
    request = requests[0]
    authorization = request.unredirected_hdrs.get("Authorization")
    assert (authorization == "Bearer synthetic-test-token") is expected_token
    if expected_token:
        redirected = transport.GitHubRedirect().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://github.com/AliceLiddell01/language-review/releases/latest",
        )
        assert redirected is not None
        assert not any(
            key.casefold() == "authorization"
            for headers in (redirected.headers, redirected.unredirected_hdrs)
            for key in headers
        )


@pytest.mark.parametrize(
    "blocker", ["commit", "asset_url", "asset_hash", "manifest_digest", "duplicate"]
)
def test_fetch_bundle_rejects_untrusted_metadata(
    bundle: Path, tmp_path: Path, monkeypatch: Any, blocker: str
) -> None:
    from language_review import release_transport as transport

    data = json.loads((bundle / "release.json").read_text("utf-8"))
    prefix = f"https://github.com/{REPOSITORY}/releases/download/{data['tag']}/"
    assets = [
        {
            "name": name,
            "browser_download_url": prefix + name,
            "digest": "sha256:" + values["sha256"],
            "size": values["size"],
        }
        for name, values in data["assets"].items()
    ]
    assets.append(
        {
            "name": "release.json",
            "browser_download_url": prefix + "release.json",
            "digest": "sha256:" + digest(bundle / "release.json"),
        }
    )
    release = {"tag_name": data["tag"], "prerelease": True, "assets": assets}
    if blocker == "asset_url":
        assets[0]["browser_download_url"] = "https://evil.test/asset"
    elif blocker == "asset_hash":
        assets[0]["digest"] = "sha256:" + "0" * 64
    elif blocker == "manifest_digest":
        assets[-1]["digest"] = ""
    elif blocker == "duplicate":
        assets.append(dict(assets[-1]))
    monkeypatch.setattr(transport, "published", lambda *a, **k: release)
    monkeypatch.setattr(
        transport, "tag_commit", lambda tag: "b" * 40 if blocker == "commit" else data["commit"]
    )
    calls = []

    def download(url: str, target: Path, *args: Any, **kwargs: Any) -> None:
        calls.append(target.name)
        assert target.name == "release.json", "Нельзя скачивать assets после отказа metadata"
        target.write_bytes((bundle / "release.json").read_bytes())

    monkeypatch.setattr(transport, "download", download)
    received = tmp_path / "received"
    received.mkdir()
    with pytest.raises(ReleaseError):
        transport.fetch_bundle(received, data["tag"])
    assert calls in ([], ["release.json"])


def test_annotated_tag_exact_ref_and_commit(monkeypatch: Any) -> None:
    from language_review import release_transport as transport

    calls = []

    def api(path: str) -> dict[str, Any]:
        calls.append(path)
        return (
            {"object": {"type": "tag", "sha": "b" * 40}}
            if path.startswith("git/ref/")
            else {"object": {"type": "commit", "sha": "a" * 40}}
        )

    monkeypatch.setattr(transport, "api", api)
    assert transport.tag_commit("v0.0.0-rc.1") == "a" * 40
    assert calls == ["git/ref/tags/v0.0.0-rc.1", "git/tags/" + "b" * 40]


def test_normal_ci_cannot_publish() -> None:
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    release = yaml.load(Path(".github/workflows/release.yml").read_text("utf-8"))
    ci = yaml.load(Path(".github/workflows/ci.yml").read_text("utf-8"))
    assert set(release["on"]) == {"workflow_dispatch"}
    assert release["permissions"] == {"contents": "read"}
    assert "contents: write" not in Path(".github/workflows/ci.yml").read_text("utf-8")
    assert ci["permissions"] == {"contents": "read"}
    assert release["jobs"]["publish"]["permissions"] == {"contents": "write", "actions": "read"}


def test_skill_uses_public_contract_without_model_or_project_writes() -> None:
    text = Path("src/language_review/distribution_assets/SKILL.md").read_text("utf-8")
    for command in (
        "config validate",
        "check <цель> --view review --format json",
        "verify-review",
        "decide-review",
        "agent:<имя/версия>",
    ):
        assert command in text
    assert all(model not in text for model in ("Luna", "Sol", "Astra", "gpt-6"))
    assert "не выполняй её" in text


def test_installation_from_checkout_is_explained(capsys: Any) -> None:
    assert main(["installation", "status"]) == 2
    assert "управляемую установку" in capsys.readouterr().err


def test_non_windows_setup_refuses_mutation(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    with pytest.raises(ReleaseError, match="Windows"):
        deploy.change_path(tmp_path / "bin", add=True)
    with pytest.raises(ReleaseError, match="Windows"):
        deploy.schedule_cleanup(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["language-review", "language-review-eval"])
def test_path_conflict_preserves_foreign_program(
    tmp_path: Path, monkeypatch: Any, name: str
) -> None:
    foreign = tmp_path / "foreign" / (name + ".cmd")
    foreign.parent.mkdir()
    foreign.write_bytes(b"foreign executable")
    monkeypatch.setattr(
        deploy.shutil, "which", lambda value: str(foreign) if value == name else None
    )
    with pytest.raises(ReleaseError, match="В PATH"):
        deploy.path_conflict(tmp_path / "application")
    assert foreign.read_bytes() == b"foreign executable"


def test_default_application_root_preserves_existing_oauth_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    oauth = tmp_path / "LanguageReview/chatgpt/accounts.dpapi"
    oauth.parent.mkdir(parents=True)
    oauth.write_bytes(b"existing encrypted user data")
    root = deploy.default_root()
    assert root == tmp_path / "LanguageReview/application"
    with deploy.locked(root):
        assert deploy.status(root) == {"status": "not_installed"}
    assert oauth.read_bytes() == b"existing encrypted user data"


@pytest.mark.skipif(os.name != "nt", reason="Контракт PowerShell Windows")
@pytest.mark.parametrize("blocker", ["schema", "lock_sha256"])
def test_memory_installer_resolves_relative_release_and_validates_manifest(
    tmp_path: Path, blocker: str
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 не найден.")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    manifest = (
        {"schema": "invalid"}
        if blocker == "schema"
        else {
            "schema": "language-review.release/1",
            "commit": "a" * 40,
            "lock_sha256": "invalid",
            "assets": {},
        }
    )
    (candidate / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

    def quote_ps(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    runner = tmp_path / "invoke-installer.ps1"
    runner.write_text(
        f"Set-Location -LiteralPath {quote_ps(tmp_path)}\n"
        f"& {quote_ps(Path('scripts/install-memory-service.ps1').resolve())} "
        f"-ReleaseDirectory 'candidate' -RuntimeDirectory {quote_ps(tmp_path / 'runtime')} "
        "-Project 'lr-relative-path-test'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(runner)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "Некорректный релизный комплект memory-worker." in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Пользовательский реестр Windows")
def test_path_roundtrip_preserves_foreign_empty_entries(tmp_path: Path, monkeypatch: Any) -> None:
    import contextlib
    import ctypes
    import winreg

    values = {"Path": "A;;B;"}
    monkeypatch.setattr(winreg, "CreateKey", lambda *args: contextlib.nullcontext(object()))
    monkeypatch.setattr(
        winreg, "QueryValueEx", lambda *args: (values["Path"], winreg.REG_EXPAND_SZ)
    )
    monkeypatch.setattr(
        winreg, "SetValueEx", lambda key, name, zero, kind, value: values.update({name: value})
    )
    monkeypatch.setattr(ctypes.windll.user32, "SendMessageTimeoutW", lambda *args: 1)
    assert deploy.change_path(tmp_path / "bin", add=True)
    assert values["Path"] == "A;;B;;" + str(tmp_path / "bin")
    assert deploy.change_path(tmp_path / "bin", add=False)
    assert values["Path"] == "A;;B;"


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_install_failure_preserves_pointer_and_skills(
    bundle: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    fake_uv = tmp_path / "uv.exe"
    fake_uv.write_bytes(b"synthetic")
    monkeypatch.setattr(deploy, "execute", lambda *a, **k: "")
    monkeypatch.setattr(deploy, "probe", lambda *a: None)
    monkeypatch.setattr(deploy, "path_conflict", lambda *a: None)
    first = deploy.install(
        bundle, root, uv=str(fake_uv), home=home, agents=["codex"], add_path=False
    )
    before = (root / "installation.json").read_bytes()
    skill = deploy.skill_path("codex", home)
    original = skill.read_bytes()

    def fail(*args: Any) -> None:
        raise ReleaseError("сбой кандидата")

    monkeypatch.setattr(deploy, "probe", fail)
    with pytest.raises(ReleaseError, match="сбой кандидата"):
        deploy.install(bundle, root, uv=str(fake_uv), extras="all", home=home, add_path=False)
    assert (root / "installation.json").read_bytes() == before
    assert skill.read_bytes() == original
    assert deploy.status(root)["version"] == first["version"]
    assert len(list((root / "releases").iterdir())) == 1
    monkeypatch.setattr(deploy, "probe", lambda *args: None)
    launcher = root / "bin/language-review.cmd"
    launcher.write_bytes(b"user launcher")
    with pytest.raises(ReleaseError, match="Launcher"):
        deploy.install(bundle, root, uv=str(fake_uv), home=home, add_path=False)
    assert launcher.read_bytes() == b"user launcher"


@pytest.fixture
def isolated_install(bundle: Path, tmp_path: Path, monkeypatch: Any) -> tuple[Path, Path, str]:
    root, home = tmp_path / "application", tmp_path / "home"
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"synthetic uv")
    monkeypatch.setattr(deploy, "execute", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(deploy, "probe", lambda *_args: None)
    monkeypatch.setattr(deploy, "path_conflict", lambda *_args: None)
    deploy.install(bundle, root, uv=str(uv), home=home, agents=["codex", "claude"], add_path=False)
    python = deploy.python_at(root / "releases" / deploy.state(root)["active"])
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"synthetic runtime")
    return root, home, str(uv)


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_install_retains_unspecified_extras_and_path_preference(
    bundle: Path, isolated_install: tuple[Path, Path, str]
) -> None:
    root, home, uv = isolated_install
    selected = deploy.install(bundle, root, uv=uv, extras="all", home=home, add_path=False)

    repeated = deploy.install(bundle, root, uv=uv, extras=None, home=home, add_path=None)

    assert repeated["active"] == selected["active"]
    assert repeated["extras"] == "all"
    assert repeated["path_requested"] is False
    assert repeated["path_added"] is False


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_journal_unlink_failure_keeps_recoverable_runtimes(
    bundle: Path, isolated_install: tuple[Path, Path, str], monkeypatch: Any
) -> None:
    root, home, uv = isolated_install
    before = (root / "installation.json").read_bytes()
    old = deploy.state(root)
    skills = {a: deploy.skill_path(a, home).read_bytes() for a in old["skills"]}
    launchers = {p: p.read_bytes() for p in deploy.launcher_files(root)}
    unlink = Path.unlink
    observed: list[str] = []

    def fail(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == root / ".transaction.json":
            observed.append(deploy.state(root)["active"])
            raise PermissionError("сбой завершения журнала")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError, match="завершения журнала"):
        deploy.install(bundle, root, uv=uv, home=home, extras="all", add_path=False)
    assert observed[0] != old["active"]
    current = deploy.state(root)
    assert (root / "releases" / current["active"]).is_dir()
    assert (root / "releases" / old["active"]).is_dir()
    for agent, content in skills.items():
        assert deploy.skill_path(agent, home).read_bytes() == content
    assert all(p.read_bytes() == content for p, content in launchers.items())
    monkeypatch.setattr(Path, "unlink", unlink)
    deploy.recover(root)
    deploy.recover(root)
    assert (root / "installation.json").read_bytes() == before
    result = deploy.install(bundle, root, uv=uv, home=home, extras="all", add_path=False)
    assert (root / "releases" / result["active"]).is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
@pytest.mark.parametrize("damage", ["bytes", "path", "inventory"])
@pytest.mark.parametrize("operation", ["update", "rollback"])
def test_update_and_rollback_reject_untrusted_uv_before_execution(
    bundle: Path,
    isolated_install: tuple[Path, Path, str],
    monkeypatch: Any,
    damage: str,
    operation: str,
    capsys: Any,
) -> None:
    root, home, uv = isolated_install
    current = deploy.install(bundle, root, uv=uv, home=home, extras="all", add_path=False)
    if damage == "bytes":
        Path(current["uv"]).write_bytes(b"tampered")
    elif damage == "path":
        current["uv"] = uv
        deploy.atomic(root / "installation.json", deploy.json_bytes(current))
    else:
        (root / "releases" / current["active"] / "owned-files.json").unlink()
    before = (root / "installation.json").read_bytes()
    calls: list[Any] = []
    monkeypatch.setattr(deploy, "execute", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(deploy, "managed_root", lambda: root)
    if operation == "rollback":
        with pytest.raises(ReleaseError):
            deploy.rollback(root)
    else:
        assert main(["installation", "update"]) == 2
        capsys.readouterr()
    assert not calls
    assert (root / "installation.json").read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
@pytest.mark.parametrize(
    "name", ["language-review.cmd", "language-review-eval.cmd", "language-review-launch.ps1"]
)
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_status_detects_launcher_drift(
    isolated_install: tuple[Path, Path, str], name: str, damage: str
) -> None:
    root, _, _ = isolated_install
    assert deploy.status(root)["status"] == "installed"
    path = root / "bin" / name
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"user edit")
    result = deploy.status(root)
    assert result["status"] == "issues"
    assert result["launchers"][name] == damage
    assert path.read_bytes() == b"user edit" if path.exists() else damage == "missing"


def test_setup_preserves_edited_managed_toml(isolated_appdata: Path) -> None:
    from language_review.config import load_config, user_config_path

    configure_endpoints({"languagetool": "http://localhost:8081"})
    target = user_config_path()
    text = (
        target.read_text("utf-8").replace(
            "[check.languagetool]", "[check.languagetool]\n# Мой тайм-аут\ntimeout_seconds = 75"
        )
        + "\n[scan]\nmax_file_size = 2000000\n"
    )
    target.write_text(text, encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ReleaseError, match="Блок setup изменён пользователем"):
        configure_endpoints({"ollama": "http://localhost:11435"})
    assert target.read_bytes() == before
    load_config(target)


def test_setup_rejects_malformed_managed_toml_without_writing(isolated_appdata: Path) -> None:
    from language_review.config import user_config_path

    configure_endpoints({"ollama": "http://localhost:11434"})
    target = user_config_path()
    target.write_text(
        "# language-review setup: начало\n[check.llm.ollama\n# language-review setup: конец\n",
        encoding="utf-8",
    )
    before = target.read_bytes()
    with pytest.raises(ReleaseError, match="синтаксически повреждён"):
        configure_endpoints({"ollama": "http://localhost:11435"})
    assert target.read_bytes() == before


def test_setup_preserves_endpoint_from_neighbor_toml_section(isolated_appdata: Path) -> None:
    from language_review.config import load_config, user_config_path

    configure_endpoints({"languagetool": "http://localhost:8081"})
    target = user_config_path()
    target.write_text(
        target.read_text("utf-8") + '\n[check.llm.ollama]\nendpoint = "http://localhost:11434"\n',
        encoding="utf-8",
    )

    configure_endpoints({"languagetool": "http://localhost:8082"})

    text = target.read_text("utf-8")
    assert '[check.llm.ollama]\nendpoint = "http://localhost:11434"' in text
    assert "http://localhost:8082" in text
    load_config(target)


def test_setup_repeated_endpoint_updates_preserve_neighbor_sections(isolated_appdata: Path) -> None:
    from language_review.config import load_config, user_config_path

    configure_endpoints({"languagetool": "http://localhost:8081"})
    target = user_config_path()
    target.write_text(
        target.read_text("utf-8") + "\n# Ручная секция\n[scan]\nmax_file_size = 12345\n",
        encoding="utf-8",
    )
    for port in (11435, 11436, 11436):
        configure_endpoints({"ollama": f"http://localhost:{port}"})
    text = target.read_text("utf-8")
    assert text.count("[check.languagetool]") == text.count("[check.llm.ollama]") == 1
    assert "# Ручная секция\n[scan]\nmax_file_size = 12345" in text
    assert "11436" in text
    load_config(target)


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_failed_recovery_preserves_selected_candidate_until_retry(
    bundle: Path,
    isolated_install: tuple[Path, Path, str],
    monkeypatch: Any,
) -> None:
    root, home, uv = isolated_install
    before = (root / "installation.json").read_bytes()
    old = deploy.state(root)
    atomic, unlink = deploy.atomic, Path.unlink
    phase = {"recovering": False}

    def fail_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == root / ".transaction.json":
            phase["recovering"] = True
            raise PermissionError("журнал занят")
        unlink(path, *args, **kwargs)

    def fail_restore(path: Path, content: bytes) -> None:
        if path == root / "installation.json" and phase["recovering"]:
            raise PermissionError("восстановление pointer заблокировано")
        atomic(path, content)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(deploy, "atomic", fail_restore)
    with pytest.raises(PermissionError, match="восстановление"):
        deploy.install(bundle, root, uv=uv, home=home, extras="all", add_path=False)
    current = deploy.state(root)
    assert current["active"] != old["active"]
    assert all(
        (root / "releases" / identifier).is_dir()
        for identifier in (current["active"], old["active"])
    )
    assert (root / ".transaction.json").is_file()
    monkeypatch.setattr(deploy, "atomic", atomic)
    monkeypatch.setattr(Path, "unlink", unlink)
    with deploy.locked(root):
        assert (root / "installation.json").read_bytes() == before
    assert not (root / ".transaction.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_status_detects_missing_active_runtime(isolated_install: tuple[Path, Path, str]) -> None:
    root, _, _ = isolated_install
    deploy.python_at(root / "releases" / deploy.state(root)["active"]).unlink()
    result = deploy.status(root)
    assert result["status"] == "issues"
    assert result["runtime"] == "missing"


def test_path_acceptance_rejects_missing_registration(tmp_path: Path, monkeypatch: Any) -> None:
    module = distribution_verifier_module()
    monkeypatch.setattr(module, "user_path_contains", lambda _: False)
    calls: list[Any] = []
    monkeypatch.setattr(module, "command", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="не зарегистрировал пользовательский PATH"):
        module.verify_registered_path(tmp_path, {"PATH": ""}, tmp_path, "1.0.0")
    assert not calls


@pytest.mark.parametrize("previous", ["none", "v1.0.0"])
def test_acceptance_reports_missing_version_cycle_honestly(
    bundle: Path,
    monkeypatch: Any,
    previous: str,
) -> None:
    from language_review import release_acceptance as acceptance

    manifest = validate_bundle(bundle)
    manifest["previous_stable"] = previous
    checks = {name: {"status": "PASS"} for name in acceptance.REQUIRED}
    evidence = acceptance.report(manifest, digest(bundle / "release.json"), checks)
    assert evidence["status"] == ("PASS" if previous == "none" else "PARTIAL")
    assert evidence["checks"]["controlled_version_upgrade_rollback"]["status"] == "NOT_RUN"
    assert "controlled_version_upgrade_rollback" in evidence["not_run"]
    assert "public_release" in evidence["not_run"]
    assert "agent_turns" in evidence["not_run"]
    assert "user_oauth" in evidence["not_run"]


@pytest.mark.skipif(os.name != "nt", reason="Контракт Windows-установки")
def test_reinstall_preserves_unspecified_skills_and_explicit_disable(
    bundle: Path, isolated_install: tuple[Path, Path, str]
) -> None:
    root, home, uv = isolated_install
    for _ in range(2):
        current = deploy.install(bundle, root, uv=uv, home=home, agents=["codex"], add_path=False)
        assert set(current["skills"]) == {"codex", "claude"}
        assert all(deploy.skill_path(a, home).is_file() for a in current["skills"])
    deploy.setup_skills(root, ["codex"])
    assert not deploy.skill_path("claude", home).exists()
    assert deploy.skill_path("codex", home).is_file()
    deploy.setup_skills(root, ["codex", "claude"])
    claude = deploy.skill_path("claude", home)
    claude.write_bytes(b"user edit")
    before = (root / "installation.json").read_bytes()
    with pytest.raises(ReleaseError):
        deploy.setup_skills(root, ["codex"])
    assert claude.read_bytes() == b"user edit"
    assert (root / "installation.json").read_bytes() == before


class WriteBoundaryReached(Exception):
    pass


@pytest.fixture
def publication(tmp_path: Path, monkeypatch: Any) -> tuple[Any, Any, dict[str, Any], list[Any]]:
    from language_review import release_acceptance as acceptance

    candidate = make_bundle(tmp_path / "publish", "1.1.0", "v1.0.0")
    notes = (
        "1.1.0 v1.0.0 "
        + "a" * 40
        + "\n"
        + "\n".join(
            title + "\nПроверенное содержимое.\n"
            for title in (
                "## Изменения",
                "## Совместимость",
                "## Миграции и откат",
                "## Ограничения",
            )
        )
    )
    (candidate / "release-notes.md").write_text(notes, encoding="utf-8")
    manifest = json.loads((candidate / "release.json").read_text("utf-8"))
    manifest["assets"]["release-notes.md"] = {
        "size": (candidate / "release-notes.md").stat().st_size,
        "sha256": digest(candidate / "release-notes.md"),
    }
    (candidate / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    checks = {name: {"status": "PASS"} for name in acceptance.REQUIRED | acceptance.VERSION_CHECKS}
    evidence = acceptance.report(
        manifest,
        digest(candidate / "release.json"),
        checks,
        {
            "tag": "v1.0.0",
            "commit": "b" * 40,
            "manifest_sha256": "c" * 64,
        },
    )
    evidence["source"] = {
        "kind": "workflow",
        "repository": REPOSITORY,
        "run_id": "123",
        "commit": "d" * 40,
        "ref": "refs/heads/main",
        "event": "workflow_dispatch",
        "workflow": ".github/workflows/release.yml",
    }
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("LR_RELEASE_AUTHORIZATION", "publish v1.1.0 " + "a" * 40)
    monkeypatch.setenv("LR_NOTES_SHA256", digest(candidate / "release-notes.md"))
    monkeypatch.setenv("LR_CANDIDATE_RUN", "123")
    module = release_module()
    monkeypatch.setattr(
        module,
        "release_history",
        lambda: [
            {
                "tag_name": "v1.0.0",
                "published_at": "date",
                "prerelease": False,
            }
        ],
    )
    calls: list[Any] = []

    def run(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        if args[:2] == ("git", "ls-remote"):
            return ""
        raise WriteBoundaryReached(args)

    def github(path: str) -> Any:
        if path.startswith("actions/runs?"):
            return {
                "workflow_runs": [
                    {
                        "id": 1,
                        "path": ".github/workflows/" + name,
                        "head_sha": "a" * 40,
                        "conclusion": "success",
                    }
                    for name in ("ci.yml", "codeql.yml")
                ]
            }
        if path == "actions/runs/123":
            return {
                "id": 123,
                "path": ".github/workflows/release.yml",
                "head_sha": "d" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "conclusion": "success",
                "head_repository": {"full_name": REPOSITORY},
            }
        if path == "releases/tags/v1.0.0":
            return {
                "tag_name": "v1.0.0",
                "published_at": "date",
                "draft": False,
                "prerelease": False,
                "assets": [{"name": "release.json", "digest": "sha256:" + "c" * 64}],
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "github", github)
    args = SimpleNamespace(candidate=candidate, evidence=tmp_path / "acceptance.json")
    return module, args, evidence, calls


@pytest.mark.parametrize(
    "damage",
    [
        "empty",
        "missing",
        "bad_value",
        "not_run",
        "missing_version",
        "no_predecessor",
        "not_run_list",
        "unknown_not_run",
        "candidate",
        "source",
        "run",
        "previous_hash",
    ],
)
def test_publish_rejects_incomplete_acceptance_before_any_github_write(
    publication: tuple[Any, Any, dict[str, Any], list[Any]],
    damage: str,
) -> None:
    module, args, evidence, calls = publication
    if damage == "empty":
        evidence["checks"] = {}
    elif damage == "missing":
        evidence["checks"].pop("core_foreign_cwd_readonly")
    elif damage == "bad_value":
        evidence["checks"]["core_foreign_cwd_readonly"] = {"status": "pass"}
    elif damage == "not_run":
        evidence["checks"]["core_foreign_cwd_readonly"] = {
            "status": "NOT_RUN",
            "reason": "unavailable",
        }
    elif damage == "missing_version":
        evidence["checks"].pop("controlled_version_upgrade_rollback")
    elif damage == "no_predecessor":
        evidence["checks"]["controlled_version_upgrade_rollback"] = {
            "status": "NOT_RUN",
            "reason": "no_predecessor",
        }
    elif damage == "not_run_list":
        evidence["not_run"] = []
    elif damage == "unknown_not_run":
        evidence["not_run"]["runtime_install"] = "unavailable"
    elif damage == "candidate":
        evidence["manifest_sha256"] = "e" * 64
    elif damage == "source":
        evidence["source"]["commit"] = "e" * 40
    elif damage == "run":
        evidence["source"]["run_id"] = "124"
    elif damage == "previous_hash":
        evidence["predecessor"]["manifest_sha256"] = "e" * 64
    args.evidence.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ReleaseError):
        module.publish(args)
    assert not any(call[0] == "gh" for call in calls)


def test_publish_complete_acceptance_reaches_intercepted_write_boundary(
    publication: tuple[Any, Any, dict[str, Any], list[Any]],
) -> None:
    module, args, evidence, calls = publication
    args.evidence.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(WriteBoundaryReached):
        module.publish(args)
    assert calls[-1][:3] == ("gh", "api", "repos/" + REPOSITORY + "/git/refs")
    assert "POST" in calls[-1]


@pytest.mark.parametrize("previous", ["none", "v1.0.0"])
def test_prepare_acceptance_uses_real_previous_candidate(
    tmp_path: Path,
    monkeypatch: Any,
    previous: str,
) -> None:
    from language_review import release_transport

    candidate = make_bundle(tmp_path / "candidate", "1.1.0", previous)
    old = make_bundle(tmp_path / "old", "1.0.0")
    fetched: list[str] = []
    calls: list[Any] = []

    def fetch(destination: Path, tag: str) -> None:
        fetched.append(tag)
        shutil.copytree(old, destination, dirs_exist_ok=True)

    module = release_module()
    monkeypatch.setattr(release_transport, "fetch_bundle", fetch)

    def run(*args: str) -> str:
        if previous != "none":
            path = Path(args[args.index("--previous-candidate") + 1])
            assert validate_bundle(path)["tag"] == previous
            assert digest(path / "release.json") == digest(old / "release.json")
        else:
            assert "--previous-candidate" not in args
        calls.append(args)
        return ""

    monkeypatch.setattr(module, "run", run)
    module.accept(SimpleNamespace(candidate=candidate, output=tmp_path / "acceptance.json"))
    assert fetched == ([] if previous == "none" else [previous])
    assert len(calls) == 1
    assert Path(calls[0][1]).name == "verify-distribution.py"


@pytest.mark.skipif(os.name != "nt", reason="Общий Windows byte-range lock")
@pytest.mark.parametrize("holder", ["python", "powershell"])
@pytest.mark.parametrize("client", ["python", "launcher"])
def test_launch_lock_waits_for_live_owner_and_survives_owner_crash(
    tmp_path: Path,
    holder: str,
    client: str,
) -> None:
    root = tmp_path / "application"
    root.mkdir()
    ready, entered = tmp_path / "ready", tmp_path / "entered"
    (root / ".launch-lock").write_bytes(b"orphan from an earlier process")
    deploy.atomic(root / "installation.json", deploy.json_bytes({"active": "a" * 32}))
    for path, content in deploy.launcher_files(root).items():
        deploy.atomic(path, content)
    python_owner = """import sys, time
from pathlib import Path
from language_review.distribution import launch_locked
with launch_locked(Path(sys.argv[1])):
    Path(sys.argv[2]).touch()
    time.sleep(60)
"""
    ps_owner = tmp_path / "owner.ps1"
    ps_owner.write_text(
        """param([string]$Root, [string]$Ready)
$gate = [IO.File]::Open((Join-Path $Root '.launch-lock'), 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
$gate.Lock(0, 1)
[IO.File]::WriteAllText($Ready, 'ready')
Start-Sleep -Seconds 60
""",
        encoding="utf-8",
    )
    owner_args = (
        [sys.executable, "-c", python_owner]
        if holder == "python"
        else ["pwsh", "-NoProfile", "-File", str(ps_owner)]
    ) + [str(root), str(ready)]
    waiter_args = (
        [
            sys.executable,
            "-c",
            """import sys
from pathlib import Path
from language_review.distribution import launch_locked
with launch_locked(Path(sys.argv[1])):
    Path(sys.argv[2]).touch()
""",
            str(root),
            str(entered),
        ]
        if client == "python"
        else [
            "pwsh",
            "-NoProfile",
            "-File",
            str(root / "bin/language-review-launch.ps1"),
            "review",
            "--version",
        ]
    )
    owner = subprocess.Popen(owner_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    waiter = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None, owner.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.03)
        waiter = subprocess.Popen(waiter_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        assert waiter.poll() is None
        assert not entered.exists()
        owner.kill()
        owner.wait(timeout=10)
        stdout, stderr = waiter.communicate(timeout=10)
        if client == "python":
            assert waiter.returncode == 0, stderr
            assert entered.exists()
        else:
            assert waiter.returncode != 0
            assert "Runtime не поддерживает" in stderr.decode("utf-8")
        with deploy.launch_locked(root):
            assert (root / ".launch-lock").exists()
    finally:
        for process in (waiter, owner):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
