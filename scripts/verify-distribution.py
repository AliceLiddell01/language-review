"""Настоящий Windows bootstrap из скачанного тестового комплекта в отдельном профиле."""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any

from language_review.release_contract import digest, validate_bundle


def command(args: list[str], env: dict[str, str], cwd: Path, codes: set[int] | None = None) -> str:
    process = subprocess.run(
        args,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if process.returncode not in (codes or {0}):
        # Только синтетические входы; штатный вывод не содержит credentials.
        raise RuntimeError(
            f"{Path(args[0]).name} {args[1:3]}: exit {process.returncode}\n"
            f"{process.stdout[-2500:]}\n{process.stderr[-2500:]}"
        )
    return process.stdout.strip()


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): digest(p) for p in root.rglob("*") if p.is_file()}


def verify(candidate: Path) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("Приёмка требует Windows/PowerShell 7.")
    manifest = validate_bundle(candidate)
    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="language-review-distribution-") as temporary:
        root = Path(temporary)
        received = root / "received"
        received.mkdir()

        # Никакого изменения доверенной production download policy: отдельный локальный
        # HTTP transport только синтетической приёмки, manifest digest передаётся извне.
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Quiet, directory=str(candidate))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for source in candidate.iterdir():
                if source.is_file():
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/{source.name}", timeout=10
                    ) as response:
                        (received / source.name).write_bytes(response.read())
            assert digest(received / "release.json") == digest(candidate / "release.json")
            validate_bundle(received)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        checks["independent_download_integrity"] = (
            "PASS (loopback release-like assets, не публичный Release)"
        )
        env = dict(os.environ)
        home = root / "profile"
        home.mkdir()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData/Roaming"),
                "LOCALAPPDATA": str(home / "AppData/Local"),
                "UV_PYTHON_INSTALL_DIR": str(root / "python"),
                "UV_NO_PROGRESS": "1",
                "UV_PYTHON_PREFERENCE": "only-managed",
            }
        )
        # Кэш Python можно использовать; product package/venv отсутствуют.
        env.pop("PYTHONPATH", None)
        env.pop("LANGUAGE_REVIEW_MEMORY_DSN", None)
        env.pop("VIRTUAL_ENV", None)
        # uv run добавляет dev venv в PATH; чистая приёмка исключает этот checkout.
        checkout_scripts = (Path.cwd() / ".venv/Scripts").resolve()
        env["PATH"] = os.pathsep.join(
            part
            for part in env["PATH"].split(os.pathsep)
            if part and Path(part).resolve() != checkout_scripts
        )
        inputs = root / "foreign"
        for name in ("repo-a", "repo-b"):
            repo = inputs / name
            repo.mkdir(parents=True)
            command(["git", "init", "--quiet", str(repo)], env, root)
            (repo / "notes.md").write_text("Русский текст документации.\n", encoding="utf-8")
        before = snapshot(inputs)
        install_root = root / "installed"
        bootstrap = [
            "pwsh",
            "-NoProfile",
            "-File",
            str(received / "install.ps1"),
            "-BundleDirectory",
            str(received),
            "-TrustedManifestSha256",
            digest(candidate / "release.json"),
            "-Root",
            str(install_root),
            "-Extras",
            "all",
            "-NonInteractive",
            "-Codex",
            "-Claude",
            "-NoPath",
        ]
        command(bootstrap, env, inputs / "repo-a")
        current = json.loads((install_root / "installation.json").read_text("utf-8"))
        python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
        cli = [str(python), "-I", "-m", "language_review"]
        assert (
            command([*cli, "--version"], env, inputs / "repo-b")
            == "language-review " + manifest["package_version"]
        )
        report = json.loads(
            command([*cli, "doctor", "--format", "json"], env, inputs / "repo-b", {0, 3})
        )
        assert report["package"]["version"] == manifest["package_version"]
        command(
            [*cli, "config", "show", "--environment", "--format", "json"], env, inputs / "repo-b"
        )
        command([*cli, "setup", "--non-interactive", "--codex", "--claude"], env, inputs / "repo-b")
        assert snapshot(inputs) == before
        checks["core_foreign_cwd_readonly"] = "PASS"
        # Проверяем реальную пользовательскую регистрацию PATH и новый pwsh; затем
        # удаляем только добавленную приёмкой запись, не восстанавливаем чужой PATH целиком.
        from language_review.distribution import change_path

        added = change_path(install_root / "bin", add=True)
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                user_path = winreg.QueryValueEx(key, "Path")[0]
            launch_env = {
                **env,
                "PATH": str(install_root / "bin")
                + os.pathsep
                + os.path.expandvars(user_path)
                + os.pathsep
                + env["PATH"],
            }
            result = command(
                ["pwsh", "-NoProfile", "-Command", "language-review --version"],
                launch_env,
                inputs / "repo-b",
            )
            assert result == "language-review " + manifest["package_version"]
        finally:
            if added:
                change_path(install_root / "bin", add=False)
        checks["user_path_new_powershell"] = "PASS"
        command(bootstrap, env, inputs / "repo-b")
        assert (
            json.loads((install_root / "installation.json").read_text("utf-8"))["active"]
            == current["active"]
        )
        checks["idempotent_install_setup"] = "PASS"
        for agent, directory in (("codex", ".agents"), ("claude", ".claude")):
            skill = home / directory / "skills/language-review/SKILL.md"
            assert skill.is_file() and manifest["package_version"] in skill.read_text("utf-8")
            original = skill.read_bytes()
            skill.write_bytes(original + b"\nuser edit\n")
            command(
                [*cli, "setup", "--non-interactive", "--codex", "--claude"],
                env,
                inputs / "repo-b",
                {2},
            )
            assert skill.read_bytes() == original + b"\nuser edit\n"
            skill.write_bytes(original)
            checks[agent + "_skill_ownership"] = "PASS"
        command(
            [*cli, "check", str(inputs / "repo-a"), "--view", "review", "--format", "json"],
            env,
            root,
            {2},
        )
        assert snapshot(inputs) == before
        checks["missing_yaml_no_creation"] = "PASS"
        command([*cli, "init", str(inputs / "repo-a")], env, root)
        policy = inputs / "repo-a/.language-review.yaml"
        policy_before = policy.read_bytes()
        command([*cli, "init", str(inputs / "repo-a")], env, root, {2})
        assert policy.read_bytes() == policy_before
        command([*cli, "config", "validate", str(inputs / "repo-a")], env, root)
        command(
            [
                *cli,
                "check",
                str(inputs / "repo-a"),
                "--view",
                "review",
                "--format",
                "json",
                "--output",
                str(root / "review.json"),
            ],
            env,
            root,
            {0, 1, 4, 5},
        )
        review = json.loads((root / "review.json").read_text("utf-8"))
        assert review["schema"] == "language-review.review"
        checks["explicit_init_check"] = "PASS (статус движков: " + review["status"] + ")"
        altered = root / "altered"
        shutil.copytree(received, altered)
        with next(altered.glob("*.whl")).open("ab") as stream:
            stream.write(b"tamper")
        bad = bootstrap.copy()
        bad[bad.index(str(received))] = str(altered)
        command(bad, env, root, {1})
        assert (
            json.loads((install_root / "installation.json").read_text("utf-8"))["active"]
            == current["active"]
        )
        assert command([*cli, "--version"], env, root).endswith(manifest["package_version"])
        checks["tampered_asset_preserves_install"] = "PASS"
        # Uninstall сохраняет неизвестные и изменённые файлы, OAuth/TOML/DB не обходятся.
        user_file = python.parent.parent / "user-note.txt"
        user_file.write_bytes(b"preserve")
        codex = home / ".agents/skills/language-review/SKILL.md"
        codex.write_bytes(codex.read_bytes() + b"\nuser edit\n")
        command([*cli, "installation", "uninstall"], env, root)
        import time

        deadline = time.monotonic() + 30
        while not (install_root / "uninstall-result.json").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Внешняя очистка uninstall не завершена.")
            time.sleep(0.2)
        assert user_file.read_bytes() == b"preserve" and codex.is_file()
        assert not (home / ".claude/skills/language-review/SKILL.md").exists()
        assert not (install_root / "bin/language-review.cmd").exists()
        checks["uninstall_ownership"] = "PASS"
        # Пользователь сам сохраняет/перемещает конфликтный skill до переустановки.
        codex.rename(codex.with_name("SKILL.saved.md"))
        command(bootstrap, env, root)
        checks["safe_reinstall"] = "PASS"
        for name, value in before.items():
            assert digest(inputs / name) == value
        assert not (inputs / "repo-b/.language-review.yaml").exists()
    return {
        "status": "PASS",
        "commit": manifest["commit"],
        "manifest_sha256": digest(candidate / "release.json"),
        "environment": "Windows/PowerShell 7/Python 3.15",
        "checks": checks,
        "not_run": [
            "публичный GitHub Release/tag",
            "обнаружение skill живыми Codex/Claude",
            "memory-worker/DB readiness и migration rollback",
            "OAuth в пользовательском аккаунте",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.candidate.absolute())
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
