"""Настоящий Windows bootstrap из скачанного тестового комплекта в отдельном профиле."""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from collections.abc import Iterator
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


@contextlib.contextmanager
def personal_skill_probe(home: Path) -> Iterator[Path]:
    # dirs::home_dir в Codex Windows использует Known Folder, а не тестовый USERPROFILE.
    # Проверяем настоящий USER scope, временно размещая только свой отсутствующий skill.
    # Ни существующий skill, ни пользовательский CODEX_HOME не перезаписываются.
    expected = Path.home() / ".agents/skills/language-review/SKILL.md"
    if expected.exists():
        raise RuntimeError(
            "Live Codex acceptance требует свободное имя личного skill language-review."
        )
    created_directory = not expected.parent.exists()
    expected.parent.mkdir(parents=True, exist_ok=True)
    content = (home / ".agents/skills/language-review/SKILL.md").read_bytes()
    with expected.open("xb") as stream:
        stream.write(content)
    try:
        yield expected
    finally:
        if expected.read_bytes() != content:
            raise RuntimeError("Тестовый личный skill изменён извне; файл сохранён.")
        expected.unlink()
        if created_directory:
            expected.parent.rmdir()


def codex_discovery(executable: Path, home: Path, cwd: Path, env: dict[str, str]) -> None:
    """Только skills/list app-server: ни thread/start, ни turn/start, ни вызова модели."""
    isolated = {**env, "CODEX_HOME": str(home / ".codex")}
    (home / ".codex").mkdir(exist_ok=True)
    messages: queue.Queue[dict[str, Any]] = queue.Queue()
    with (
        personal_skill_probe(home) as expected,
        (home / "codex-server.stderr").open("w", encoding="utf-8") as errors,
    ):
        process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            cwd=cwd,
            env=isolated,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
        )
        assert process.stdin is not None and process.stdout is not None

        def receive() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def request(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps({"id": identifier, "method": method, "params": params}) + "\n"
            )
            process.stdin.flush()
            while True:
                response = messages.get(timeout=30)
                if response.get("id") == identifier:
                    if "error" in response:
                        raise RuntimeError("Codex app-server отказал в read-only skills/list.")
                    return dict(response["result"])

        try:
            request(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "language-review-acceptance", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            process.stdin.write('{"method":"initialized"}\n')
            process.stdin.flush()
            result = request(2, "skills/list", {"cwds": [str(cwd)], "forceReload": True})
            skills = [skill for entry in result["data"] for skill in entry["skills"]]
            assert any(
                skill["name"] == "language-review"
                and Path(skill["path"]).resolve() == expected.resolve()
                for skill in skills
            ), "Codex skills/list: " + json.dumps(
                {
                    "expected": str(expected),
                    "matches": [s for s in skills if s["name"] == "language-review"],
                    "errors": [entry.get("errors", []) for entry in result["data"]],
                    "discovered": [
                        {"name": s["name"], "path": s["path"], "scope": s.get("scope")}
                        for s in skills
                    ],
                },
                ensure_ascii=False,
            )
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            reader.join(timeout=5)


def verify(
    candidate: Path, codex_executable: Path | None = None, upgrade_candidate: Path | None = None
) -> dict[str, Any]:
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
        from language_review.chatgpt_store import dpapi

        account_file = Path(env["LOCALAPPDATA"]) / "LanguageReview/chatgpt/accounts.dpapi"
        account_file.parent.mkdir(parents=True)
        protected_session = dpapi(b"synthetic encrypted session; no real OAuth")
        account_file.write_bytes(protected_session)
        user_config = Path(env["APPDATA"]) / "language-review/config.toml"
        user_config.parent.mkdir(parents=True)
        user_environment = b'[check.llm.ollama]\nendpoint = "http://127.0.0.1:11434"\n'
        user_config.write_bytes(user_environment)
        # uv run добавляет dev venv в PATH; чистая приёмка исключает этот checkout.
        checkout_scripts = (Path.cwd() / ".venv/Scripts").resolve()
        existing_uv = shutil.which("uv")
        uv_scripts = Path(existing_uv).resolve().parent if existing_uv else None
        env["PATH"] = os.pathsep.join(
            part
            for part in env["PATH"].split(os.pathsep)
            if part and Path(part).resolve() not in {checkout_scripts, uv_scripts}
        )
        inputs = root / "foreign"
        for name in ("repo-a", "repo-b"):
            repo = inputs / name
            repo.mkdir(parents=True)
            command(["git", "init", "--quiet", str(repo)], env, root)
            (repo / "notes.md").write_text("Русский текст документации.\n", encoding="utf-8")
        before = snapshot(inputs)
        install_root = Path(env["LOCALAPPDATA"]) / "LanguageReview/application"
        bootstrap = [
            "pwsh",
            "-NoProfile",
            "-File",
            str(received / "install.ps1"),
            "-BundleDirectory",
            str(received),
            "-TrustedManifestSha256",
            digest(candidate / "release.json"),
            "-Extras",
            "core",
            "-NonInteractive",
            "-Codex",
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
        assert not (home / ".claude/skills/language-review").exists()
        checks["claude_not_selected_absent"] = "PASS"
        command(
            [*cli, "config", "show", "--environment", "--format", "json"], env, inputs / "repo-b"
        )
        command([*cli, "setup", "--non-interactive", "--codex"], env, inputs / "repo-b")
        assert snapshot(inputs) == before
        checks["core_foreign_cwd_readonly"] = "PASS"
        # Extras включаются явно отдельным проверяемым upgrade той же поставки.
        bootstrap[bootstrap.index("core")] = "all"
        bootstrap.append("-Claude")
        command(bootstrap, env, inputs / "repo-b")
        updated = json.loads((install_root / "installation.json").read_text("utf-8"))
        assert updated["active"] != current["active"] and updated["previous"] == current["active"]
        current = updated
        python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
        cli = [str(python), "-I", "-m", "language_review"]
        checks["controlled_extras_upgrade"] = "PASS"
        # Sdist того же релизного SHA устанавливается отдельно и сверяет тот же CLI.
        uv = current["uv"]
        sdist_env = root / "sdist-env"
        command([uv, "venv", "--no-config", "--python", "3.15", str(sdist_env)], env, root)
        sdist_python = str(sdist_env / "Scripts/python.exe")
        command(
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                sdist_python,
                "--no-index",
                "--find-links",
                str(install_root / "releases" / current["active"] / "wheelhouse"),
                "--require-hashes",
                "-r",
                str(received / "requirements-core.txt"),
            ],
            env,
            root,
        )
        command(
            [
                uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                sdist_python,
                "--no-deps",
                str(next(received.glob("*.tar.gz"))),
            ],
            env,
            root,
        )
        assert (
            command(
                [sdist_python, "-I", "-m", "language_review", "--version"], env, inputs / "repo-b"
            )
            == "language-review " + manifest["package_version"]
        )
        checks["release_sdist_independent_install"] = "PASS"
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
            evaluator = command(
                ["pwsh", "-NoProfile", "-Command", "language-review-eval --help"],
                launch_env,
                inputs / "repo-b",
            )
            assert "language-review-eval" in evaluator
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
        if codex_executable:
            codex_discovery(
                codex_executable,
                home,
                inputs / "repo-b",
                {**env, "PATH": str(install_root / "bin") + os.pathsep + env["PATH"]},
            )
            checks["codex_live_discovery"] = "PASS (app-server skills/list, модель не запускалась)"
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
        if shutil.which("vale", path=env["PATH"]):
            policy.write_text(
                "schema_version: 1\ndefault_profile: docs\n"
                "profiles:\n  docs:\n    engines: [vale]\n",
                encoding="utf-8",
            )
            source = inputs / "repo-a/notes.md"
            original = source.read_bytes()
            source.write_text("Слово слово повторяется.\n", encoding="utf-8")
            command(
                [
                    *cli,
                    "check",
                    str(source),
                    "--view",
                    "review",
                    "--format",
                    "json",
                    "--output",
                    str(root / "positive.json"),
                ],
                env,
                root,
                {1},
            )
            positive = json.loads((root / "positive.json").read_text("utf-8"))
            assert positive["status"] == "findings" and positive["items"]
            command(
                [
                    *cli,
                    "verify-review",
                    str(root / "positive.json"),
                    "--root",
                    "root-0001=" + str(source),
                    "--format",
                    "json",
                ],
                env,
                root,
            )
            source.write_bytes(original)
            command(
                [
                    *cli,
                    "verify-review",
                    str(root / "positive.json"),
                    "--root",
                    "root-0001=" + str(source),
                    "--format",
                    "json",
                ],
                env,
                root,
                {4},
            )
            clean = json.loads(
                command(
                    [*cli, "check", str(source), "--view", "review", "--format", "json"], env, root
                )
            )
            assert clean["status"] == "clean"
            checks["real_vale_findings_clean_stale_hash"] = "PASS"
        if upgrade_candidate:
            upgrade = validate_bundle(upgrade_candidate)
            damaged = root / "failing-candidate"
            shutil.copytree(upgrade_candidate, damaged)
            wheel_path = next(damaged.glob("*.whl"))
            import zipfile

            with zipfile.ZipFile(wheel_path) as archive:
                entries = {entry.filename: archive.read(entry) for entry in archive.infolist()}
            entries["language_review/__main__.py"] = (
                b'raise RuntimeError("synthetic candidate failure")\n'
            )
            with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)
            damaged_manifest = json.loads((damaged / "release.json").read_text("utf-8"))
            damaged_manifest["assets"][wheel_path.name] = {
                "size": wheel_path.stat().st_size,
                "sha256": digest(wheel_path),
            }
            (damaged / "release.json").write_text(json.dumps(damaged_manifest), encoding="utf-8")
            failure = bootstrap.copy()
            failure[failure.index(str(received))] = str(damaged)
            failure[failure.index(digest(candidate / "release.json"))] = digest(
                damaged / "release.json"
            )
            state_before = (install_root / "installation.json").read_bytes()
            command(failure, env, root, {1})
            assert (install_root / "installation.json").read_bytes() == state_before
            checks["real_failing_candidate_preserves_previous"] = "PASS"
            success = bootstrap.copy()
            success[success.index(str(received))] = str(upgrade_candidate)
            success[success.index(digest(candidate / "release.json"))] = digest(
                upgrade_candidate / "release.json"
            )
            command(success, env, root)
            assert (
                json.loads((install_root / "installation.json").read_text("utf-8"))["version"]
                == upgrade["version"]
            )
            command([*cli, "installation", "rollback"], env, root)
            current = json.loads((install_root / "installation.json").read_text("utf-8"))
            assert current["version"] == manifest["version"]
            python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
            cli = [str(python), "-I", "-m", "language_review"]
            assert command([*cli, "--version"], env, root).endswith(manifest["package_version"])
            checks["controlled_version_upgrade_rollback"] = "PASS"
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

        deadline = time.monotonic() + 180
        while (
            not (install_root / "uninstall-result.json").exists()
            or (install_root / ".install-lock").exists()
        ):
            if time.monotonic() > deadline:
                raise RuntimeError("Внешняя очистка uninstall не завершена.")
            time.sleep(0.2)
        assert (
            json.loads((install_root / "uninstall-result.json").read_text("utf-8"))["status"]
            == "uninstalled"
        )
        assert user_file.read_bytes() == b"preserve" and codex.is_file()
        assert not (home / ".claude/skills/language-review/SKILL.md").exists()
        assert not (install_root / "bin/language-review.cmd").exists()
        checks["uninstall_ownership"] = "PASS"
        # Пользователь сам сохраняет/перемещает конфликтный skill до переустановки.
        codex.rename(codex.with_name("SKILL.saved.md"))
        command(bootstrap, env, root)
        checks["safe_reinstall"] = "PASS"
        assert account_file.read_bytes() == protected_session
        assert (
            dpapi(account_file.read_bytes(), decrypt=True)
            == b"synthetic encrypted session; no real OAuth"
        )
        assert user_config.read_bytes() == user_environment
        checks["dpapi_environment_preserved"] = (
            "PASS (синтетический DPAPI payload, без OAuth login)"
        )
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
            "вызов skill моделью Claude; выполнение агентских turn не запускалось",
            "memory-worker/DB readiness и migration rollback",
            "OAuth в пользовательском аккаунте",
        ]
        + ([] if codex_executable else ["обнаружение пользовательского skill Codex app-server"])
        + ([] if upgrade_candidate else ["смена версии и rollback (нужен --upgrade-candidate)"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--upgrade-candidate", type=Path)
    args = parser.parse_args()
    result = verify(args.candidate.absolute(), args.codex_executable, args.upgrade_candidate)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
