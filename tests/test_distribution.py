from __future__ import annotations

import importlib.util
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from language_review import distribution as deploy
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
    root = tmp_path / "bundle"
    root.mkdir()
    version = "0.0.0-rc.1"
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
    for name in (
        "install.ps1",
        "release-notes.md",
        "requirements-core.txt",
        "requirements-chatgpt.txt",
        "requirements-memory.txt",
        "requirements-all.txt",
        *RUNTIME_ASSETS,
    ):
        (root / name).write_text("Синтетический контрактный вход\n", encoding="utf-8")
    manifest = {
        "schema": "language-review.release/1",
        "repository": REPOSITORY,
        **identity,
        "channel": "prerelease",
        "previous_stable": "none",
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
    "name", ["../evil.ps1", "x/evil.ps1", "x\\evil.ps1", "C:evil.ps1", "a..b", "evil.ps1:stream"]
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


def release_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_script", "scripts/release.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    for version, commit, previous in (
        ("0.2.0-rc.1", "a" * 40, "v0.1.0"),
        ("0.1.1", "a" * 40, "v0.1.0"),
        ("0.2.0", "main", "v0.1.0"),
        ("0.2.0", "a" * 40, "v0.2.0-rc.1"),
    ):
        with pytest.raises(ReleaseError):
            module.check_target(version, commit, previous, history)


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
