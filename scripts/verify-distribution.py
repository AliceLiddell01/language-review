"""Настоящая установка Windows из скачанного тестового комплекта в отдельном профиле."""

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
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from language_review import release_acceptance as acceptance
from language_review.release_contract import digest, validate_bundle, version_key


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
        # Только синтетические входы; штатный вывод не содержит учётных данных.
        raise RuntimeError(
            f"{Path(args[0]).name} {args[1:3]}: exit {process.returncode}\n"
            f"{process.stdout[-2500:]}\n{process.stderr[-2500:]}"
        )
    return process.stdout.strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("FAIL: " + message)


def _without_path_directories(path: str, excluded: set[Path]) -> str:
    blocked = {os.path.normcase(str(directory.resolve())) for directory in excluded}
    return os.pathsep.join(
        part
        for part in path.split(os.pathsep)
        if part and os.path.normcase(str(Path(part).resolve())) not in blocked
    )


def isolated_environments(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    checkout_scripts = (Path(sys.prefix) / "Scripts").resolve()
    runtime_path = _without_path_directories(env["PATH"], {checkout_scripts})
    runtime_env = {**env, "PATH": runtime_path}
    tools = ("python", "python3", "pythonw", "uv", "pwsh", "powershell")
    excluded = {
        Path(part)
        for part in runtime_path.split(os.pathsep)
        if part and any(shutil.which(name, path=part) for name in tools)
    }
    bootstrap_env = {
        **env,
        "PATH": _without_path_directories(runtime_path, excluded),
    }
    require(
        not any(shutil.which(name, path=bootstrap_env["PATH"]) for name in tools),
        "Bootstrap PATH содержит системный Python, uv или shell.",
    )
    return runtime_env, bootstrap_env


def bootstrap_for_bundle(bootstrap: list[str], bundle: Path) -> list[str]:
    result = bootstrap.copy()
    result[0] = str(bundle / "install.exe")
    result[result.index("--bundle-directory") + 1] = str(bundle)
    result[result.index("--trusted-manifest-sha256") + 1] = digest(bundle / "release.json")
    return result


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
    profile = Path.home()
    missing: list[Path] = []
    directory = expected.parent
    while directory != profile and directory.is_relative_to(profile):
        missing.append(directory)
        directory = directory.parent
    created_directories: list[Path] = []
    created_file = False
    try:
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                if not directory.is_dir():
                    raise
            else:
                created_directories.append(directory)
        content = (home / ".agents/skills/language-review/SKILL.md").read_bytes()
        try:
            with expected.open("xb") as stream:
                created_file = True
                stream.write(content)
            yield expected
        finally:
            if created_file and expected.exists():
                if (
                    expected.is_symlink()
                    or expected.is_junction()
                    or expected.read_bytes() != content
                ):
                    raise RuntimeError("Тестовый личный skill изменён извне; файл сохранён.")
                expected.unlink()
    finally:
        if not expected.exists():
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except FileNotFoundError:
                    pass


def require_upgrade_version(current: dict[str, Any], upgrade: dict[str, Any]) -> None:
    require(
        version_key(upgrade["version"]) > version_key(current["version"]),
        "--upgrade-candidate должен иметь более новую версию.",
    )


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
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            raise RuntimeError("FAIL: Codex app-server не создал каналы ввода и вывода.")

        def receive() -> None:
            for line in stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def request(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            stdin.flush()
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
            stdin.write('{"method":"initialized"}\n')
            stdin.flush()
            result = request(2, "skills/list", {"cwds": [str(cwd)], "forceReload": True})
            skills = [skill for entry in result["data"] for skill in entry["skills"]]
            require(
                any(
                    skill["name"] == "language-review"
                    and Path(skill["path"]).resolve() == expected.resolve()
                    for skill in skills
                ),
                "Codex skills/list: "
                + json.dumps(
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
                ),
            )
        finally:
            stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            reader.join(timeout=5)


def user_path_contains(bin_path: Path) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value = winreg.QueryValueEx(key, "Path")[0]
    except FileNotFoundError:
        return False
    return any(
        os.path.normcase(os.path.expandvars(part)) == os.path.normcase(str(bin_path))
        for part in value.split(";")
    )


def verify_registered_path(
    install_root: Path, env: dict[str, str], cwd: Path, version: str
) -> None:
    require(
        user_path_contains(install_root / "bin"),
        "Установщик не зарегистрировал пользовательский PATH.",
    )
    # Новый дочерний процесс читает постоянные области Windows. Родитель не
    # добавляет тестовый bin в PATH и не подменяет результат поиска программы.
    probe = r"""import json, os, shutil, subprocess, winreg
parts = []
machine = r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
for hive, path in ((winreg.HKEY_LOCAL_MACHINE, machine), (winreg.HKEY_CURRENT_USER, 'Environment')):
    try:
        with winreg.OpenKey(hive, path) as key:
            parts.append(os.path.expandvars(winreg.QueryValueEx(key, 'Path')[0]))
    except FileNotFoundError:
        pass
os.environ['PATH'] = os.pathsep.join(parts)
review = shutil.which('language-review.exe')
evaluator = shutil.which('language-review-eval.exe')
if not review or not evaluator:
    raise RuntimeError('CLI/evaluator отсутствует в постоянном PATH')
options = dict(check=True, capture_output=True, text=True, encoding='utf-8', timeout=300)
version = subprocess.run([review, '--version'], **options).stdout.strip()
help_text = subprocess.run([evaluator, '--help'], **options).stdout
print(json.dumps(dict(review=review, evaluator=evaluator, version=version, help=help_text)))
"""
    result = json.loads(command([sys.executable, "-I", "-c", probe], env, cwd))
    require(
        Path(result["review"]).resolve() == (install_root / "bin/language-review.exe").resolve(),
        "PATH выбрал посторонний CLI.",
    )
    require(
        Path(result["evaluator"]).resolve()
        == (install_root / "bin/language-review-eval.exe").resolve(),
        "PATH выбрал посторонний evaluator.",
    )
    require(
        result["version"] == "language-review " + version,
        "Команда из PATH запустила неверную версию.",
    )
    require("language-review-eval" in result["help"], "Evaluator из PATH не вернул справку.")


def verify_vale(
    cli: list[str],
    env: dict[str, str],
    root: Path,
    inputs: Path,
    checks: dict[str, dict[str, str]],
) -> None:
    """Проверяет настоящий Vale; отсутствие программы не считается успехом."""
    if shutil.which("vale", path=env["PATH"]):
        policy = inputs / "repo-a/.language-review.yaml"
        policy.write_text(
            "schema_version: 1\ndefault_profile: docs\nprofiles:\n  docs:\n    engines: [vale]\n",
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
        require(
            positive["status"] == "findings" and positive["items"],
            "Vale не обнаружил синтетическую языковую ошибку.",
        )
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
            command([*cli, "check", str(source), "--view", "review", "--format", "json"], env, root)
        )
        require(clean["status"] == "clean", "Vale оставил finding после восстановления входа.")
        checks["real_vale_findings_clean_stale_hash"] = {"status": "PASS"}
    else:
        checks["real_vale_findings_clean_stale_hash"] = {
            "status": "NOT_RUN",
            "reason": "vale_unavailable",
        }


def verify_version_cycle(
    initial: Path,
    upgrade_candidate: Path,
    bootstrap: list[str],
    install_root: Path,
    env: dict[str, str],
    root: Path,
) -> dict[str, dict[str, str]]:
    manifest = validate_bundle(initial)
    checks: dict[str, dict[str, str]] = {}
    upgrade = validate_bundle(upgrade_candidate)
    require_upgrade_version(manifest, upgrade)
    damaged = root / "failing-candidate"
    shutil.copytree(upgrade_candidate, damaged)
    wheel_path = next(damaged.glob("*.whl"))
    import zipfile

    with zipfile.ZipFile(wheel_path) as archive:
        entries = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    entries["language_review/__main__.py"] = b'raise RuntimeError("synthetic candidate failure")\n'
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    damaged_manifest = json.loads((damaged / "release.json").read_text("utf-8"))
    damaged_manifest["assets"][wheel_path.name] = {
        "size": wheel_path.stat().st_size,
        "sha256": digest(wheel_path),
    }
    (damaged / "release.json").write_text(json.dumps(damaged_manifest), encoding="utf-8")
    failure = bootstrap_for_bundle(bootstrap, damaged)
    state_before = (install_root / "installation.json").read_bytes()
    failure_output = subprocess.run(
        failure,
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    failure_details = failure_output.stdout + failure_output.stderr
    require(
        failure_output.returncode == 1,
        "Установщик должен отклонить кандидата с ошибкой readiness.",
    )
    require(
        "synthetic candidate failure" in failure_details,
        "Кандидат должен запуститься и завершиться синтетической ошибкой readiness.",
    )
    require(
        (install_root / "installation.json").read_bytes() == state_before,
        "Неудачный кандидат изменил текущий installation pointer.",
    )
    checks["real_failing_candidate_preserves_previous"] = {"status": "PASS"}
    success = bootstrap_for_bundle(bootstrap, upgrade_candidate)
    command(success, env, root)
    require(
        json.loads((install_root / "installation.json").read_text("utf-8"))["version"]
        == upgrade["version"],
        "Upgrade не установил ожидаемую версию.",
    )
    upgraded_state = json.loads((install_root / "installation.json").read_text("utf-8"))
    upgraded_python = (
        install_root / "releases" / upgraded_state["active"] / "venv/Scripts/python.exe"
    )
    command(
        [str(upgraded_python), "-I", "-m", "language_review", "installation", "rollback"], env, root
    )
    current = json.loads((install_root / "installation.json").read_text("utf-8"))
    require(current["version"] == manifest["version"], "Rollback не восстановил прежнюю версию.")
    cli = [str(install_root / "bin/language-review.exe")]
    require(
        command([*cli, "--version"], env, root).endswith(manifest["package_version"]),
        "CLI после rollback сообщил неверную версию.",
    )
    checks["controlled_version_upgrade_rollback"] = {"status": "PASS"}
    return checks


def verify(
    candidate: Path,
    codex_executable: Path | None = None,
    upgrade_candidate: Path | None = None,
    previous_candidate: Path | None = None,
) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("Приёмка требует Windows/Python 3.15.")
    manifest = validate_bundle(candidate)
    checks: dict[str, dict[str, str]] = {}
    predecessor = None
    require(
        not (upgrade_candidate and previous_candidate),
        "Выберите один вариант проверки смены версии.",
    )
    with (
        tempfile.TemporaryDirectory(prefix="language-review-distribution-") as temporary,
        contextlib.ExitStack() as cleanup,
    ):
        root = Path(temporary)
        received = root / "received"
        received.mkdir()

        # Никакого изменения политики доверенной загрузки продукта: отдельный локальный
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
            require(
                digest(received / "release.json") == digest(candidate / "release.json"),
                "Loopback manifest отличается от выбранного кандидата.",
            )
            validate_bundle(received)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        checks["independent_download_integrity"] = {
            "status": "PASS",
            "detail": "Загрузка через loopback; публичный Release не проверялся.",
        }
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
        # Bootstrap запускается без системных Python, uv и shell в PATH.
        env, bootstrap_env = isolated_environments(env)
        inputs = root / "foreign"
        for name in ("repo-a", "repo-b"):
            repo = inputs / name
            repo.mkdir(parents=True)
            command(["git", "init", "--quiet", str(repo)], env, root)
            (repo / "notes.md").write_text("Русский текст документации.\n", encoding="utf-8")
        before = snapshot(inputs)
        install_root = Path(env["LOCALAPPDATA"]) / "LanguageReview/application"
        bootstrap = [
            str(received / "install.exe"),
            "--bundle-directory",
            str(received),
            "--trusted-manifest-sha256",
            digest(candidate / "release.json"),
            "--extras",
            "core",
            "--non-interactive",
            "--codex",
        ]
        from language_review.distribution import change_path

        require(
            not user_path_contains(install_root / "bin"),
            "Тестовый каталог уже зарегистрирован в PATH.",
        )
        cleanup.callback(change_path, install_root / "bin", add=False)
        command(bootstrap, bootstrap_env, inputs / "repo-a")
        shell_extensions = {".ps1", ".psm1", ".psd1", ".cmd", ".bat", ".sh", ".bash", ".zsh"}
        active_runtime = (
            install_root
            / "releases"
            / json.loads((install_root / "installation.json").read_text("utf-8"))["active"]
        )
        require(
            not any(
                path.suffix.casefold() in shell_extensions
                for directory in (
                    install_root / "bin",
                    active_runtime / "bundle",
                    active_runtime / "venv/Scripts",
                )
                for path in directory.rglob("*")
            ),
            "Установка создала shell-сценарий.",
        )
        for name in ("language-review.exe", "language-review-eval.exe"):
            require(
                (install_root / "bin" / name).read_bytes()
                == (received / "install.exe").read_bytes(),
                "Launcher не соответствует проверенному автономному bootstrap.",
            )
        checks["shell_free_bootstrap_and_launchers"] = {"status": "PASS"}
        require(
            command(
                [str(install_root / "bin/language-review.exe"), "--version"],
                bootstrap_env,
                inputs / "repo-b",
            )
            == "language-review " + manifest["package_version"],
            "Автономный launcher сообщил неверную версию.",
        )
        command(
            [str(install_root / "bin/language-review-eval.exe"), "--help"],
            bootstrap_env,
            inputs / "repo-b",
        )
        verify_registered_path(install_root, env, inputs / "repo-b", manifest["package_version"])
        require(
            change_path(install_root / "bin", add=False), "Не удалось снять тестовую запись PATH."
        )
        try:
            verify_registered_path(
                install_root, env, inputs / "repo-b", manifest["package_version"]
            )
        except RuntimeError as exc:
            require(
                "не зарегистрировал" in str(exc),
                "Отказ PATH должен объяснять отсутствие регистрации.",
            )
        else:
            raise RuntimeError("Отсутствующая регистрация PATH дала ложный успех.")
        command(bootstrap, bootstrap_env, inputs / "repo-a")
        verify_registered_path(install_root, env, inputs / "repo-b", manifest["package_version"])
        current = json.loads((install_root / "installation.json").read_text("utf-8"))
        python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
        cli = [str(install_root / "bin/language-review.exe")]
        require(
            command([*cli, "--version"], env, inputs / "repo-b")
            == "language-review " + manifest["package_version"],
            "Установленный CLI сообщил неверную версию.",
        )
        report = json.loads(
            command([*cli, "doctor", "--format", "json"], env, inputs / "repo-b", {0, 3})
        )
        require(
            report["package"]["version"] == manifest["package_version"],
            "Диагностика сообщает неверную версию пакета.",
        )
        require(
            not (home / ".claude/skills/language-review").exists(),
            "Claude skill появился без явного выбора.",
        )
        checks["claude_not_selected_absent"] = {"status": "PASS"}
        command(
            [*cli, "config", "show", "--environment", "--format", "json"], env, inputs / "repo-b"
        )
        command([*cli, "setup", "--non-interactive", "--codex"], env, inputs / "repo-b")
        require(snapshot(inputs) == before, "Команды чтения изменили входные репозитории.")
        checks["core_foreign_cwd_readonly"] = {"status": "PASS"}
        # Extras включаются явно отдельным проверяемым upgrade той же поставки.
        bootstrap[bootstrap.index("core")] = "all"
        bootstrap.append("--claude")
        command(bootstrap, bootstrap_env, inputs / "repo-b")
        updated = json.loads((install_root / "installation.json").read_text("utf-8"))
        require(
            updated["active"] != current["active"] and updated["previous"] == current["active"],
            "Обновление extras не переключило runtime атомарно.",
        )
        current = updated
        python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
        cli = [str(install_root / "bin/language-review.exe")]
        checks["controlled_extras_upgrade"] = {"status": "PASS"}
        codex_only = [arg for arg in bootstrap if arg != "--claude"]
        command(codex_only, bootstrap_env, inputs / "repo-b")
        require(
            json.loads((install_root / "installation.json").read_text("utf-8"))["skills"]
            == current["skills"],
            "Повторный выбор Codex снял ранее выбранный Claude skill.",
        )
        no_path_bootstrap = bootstrap.copy()
        no_path_bootstrap.append("--no-path")
        command(no_path_bootstrap, bootstrap_env, inputs / "repo-b")
        no_path_state = json.loads((install_root / "installation.json").read_text("utf-8"))
        require(
            no_path_state["extras"] == "all" and no_path_state["path_requested"] is False,
            "Явный --no-path не сохранил состояние установки.",
        )
        default_bootstrap = no_path_bootstrap.copy()
        for flag in ("--codex", "--claude", "--no-path"):
            default_bootstrap.remove(flag)
        extras_index = default_bootstrap.index("--extras")
        del default_bootstrap[extras_index : extras_index + 2]
        command(default_bootstrap, bootstrap_env, inputs / "repo-b")
        default_state = json.loads((install_root / "installation.json").read_text("utf-8"))
        require(
            default_state["active"] == no_path_state["active"]
            and default_state["extras"] == "all"
            and default_state["path_requested"] is False
            and default_state["path_added"] == no_path_state["path_added"],
            "Повторный bootstrap без параметров изменил extras или регистрацию PATH.",
        )
        current = default_state
        python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
        cli = [str(install_root / "bin/language-review.exe")]
        checks["default_bootstrap_preserves_extras_and_path"] = {"status": "PASS"}
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
        require(
            command(
                [sdist_python, "-I", "-m", "language_review", "--version"], env, inputs / "repo-b"
            )
            == "language-review " + manifest["package_version"],
            "Sdist CLI сообщил неверную версию.",
        )
        checks["release_sdist_independent_install"] = {"status": "PASS"}
        verify_registered_path(install_root, env, inputs / "repo-b", manifest["package_version"])
        checks["installer_user_path_new_process"] = {"status": "PASS"}
        command(bootstrap, bootstrap_env, inputs / "repo-b")
        require(
            json.loads((install_root / "installation.json").read_text("utf-8"))["active"]
            == current["active"],
            "Повторная установка неожиданно сменила active runtime.",
        )
        checks["idempotent_install_setup"] = {"status": "PASS"}
        if codex_executable:
            codex_discovery(
                codex_executable,
                home,
                inputs / "repo-b",
                {**env, "PATH": str(install_root / "bin") + os.pathsep + env["PATH"]},
            )
            checks["codex_live_discovery"] = {
                "status": "PASS",
                "detail": "app-server skills/list, модель не запускалась",
            }
        for agent, directory in (("codex", ".agents"), ("claude", ".claude")):
            skill = home / directory / "skills/language-review/SKILL.md"
            require(
                skill.is_file() and manifest["package_version"] in skill.read_text("utf-8"),
                f"Установленный skill не соответствует версии пакета: {agent}.",
            )
            original = skill.read_bytes()
            skill.write_bytes(original + b"\nuser edit\n")
            command(
                [*cli, "setup", "--non-interactive", "--codex", "--claude"],
                env,
                inputs / "repo-b",
                {2},
            )
            require(
                skill.read_bytes() == original + b"\nuser edit\n",
                f"Setup перезаписал пользовательский skill: {agent}.",
            )
            skill.write_bytes(original)
            checks[agent + "_skill_ownership"] = {"status": "PASS"}
        command(
            [*cli, "check", str(inputs / "repo-a"), "--view", "review", "--format", "json"],
            env,
            root,
            {2},
        )
        require(snapshot(inputs) == before, "Проверка без YAML изменила входные репозитории.")
        checks["missing_yaml_no_creation"] = {"status": "PASS"}
        command([*cli, "init", str(inputs / "repo-a")], env, root)
        policy = inputs / "repo-a/.language-review.yaml"
        policy_before = policy.read_bytes()
        command([*cli, "init", str(inputs / "repo-a")], env, root, {2})
        require(policy.read_bytes() == policy_before, "Повторная init перезаписала YAML.")
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
        require(review["schema"] == "language-review.review", "Review вернул неверную схему.")
        checks["explicit_init_check"] = {
            "status": "PASS",
            "detail": "Статус движков: " + review["status"],
        }
        verify_vale(cli, env, root, inputs, checks)
        if previous_candidate:
            previous = validate_bundle(previous_candidate)
            require(
                previous["tag"] == manifest["previous_stable"] and previous["channel"] == "stable",
                "Предыдущий кандидат не соответствует previous_stable.",
            )
            require_upgrade_version(previous, manifest)
            predecessor = {
                "tag": previous["tag"],
                "commit": previous["commit"],
                "manifest_sha256": digest(previous_candidate / "release.json"),
            }
            cycle_env = dict(env)
            cycle_bootstrap_env = dict(bootstrap_env)
            for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                cycle_env[key] = str(root / "previous-profile" / key)
                Path(cycle_env[key]).mkdir(parents=True, exist_ok=True)
                cycle_bootstrap_env[key] = cycle_env[key]
            cycle_root = Path(cycle_env["LOCALAPPDATA"]) / "LanguageReview/application"
            previous_bootstrap = [
                str(previous_candidate / "install.exe"),
                "--bundle-directory",
                str(previous_candidate),
                "--trusted-manifest-sha256",
                digest(previous_candidate / "release.json"),
                "--non-interactive",
                "--no-path",
            ]
            command(previous_bootstrap, cycle_bootstrap_env, root)
            cycle_bootstrap = [
                str(received / "install.exe"),
                "--bundle-directory",
                str(received),
                "--trusted-manifest-sha256",
                digest(received / "release.json"),
                "--non-interactive",
                "--no-path",
            ]
            checks.update(
                verify_version_cycle(
                    previous_candidate,
                    candidate,
                    cycle_bootstrap,
                    cycle_root,
                    cycle_bootstrap_env,
                    root,
                )
            )
        if upgrade_candidate:
            checks.update(
                verify_version_cycle(
                    received, upgrade_candidate, bootstrap, install_root, bootstrap_env, root
                )
            )
            current = json.loads((install_root / "installation.json").read_text("utf-8"))
            python = install_root / "releases" / current["active"] / "venv/Scripts/python.exe"
            cli = [str(install_root / "bin/language-review.exe")]
        altered = root / "altered"
        shutil.copytree(received, altered)
        with next(altered.glob("*.whl")).open("ab") as stream:
            stream.write(b"tamper")
        bad = bootstrap.copy()
        bad[bad.index(str(received))] = str(altered)
        command(bad, bootstrap_env, root, {1})
        require(
            json.loads((install_root / "installation.json").read_text("utf-8"))["active"]
            == current["active"],
            "Повреждённый bundle изменил active runtime.",
        )
        require(
            command([*cli, "--version"], env, root).endswith(manifest["package_version"]),
            "CLI после отказа повреждённого bundle сообщил неверную версию.",
        )
        checks["tampered_asset_preserves_install"] = {"status": "PASS"}
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
        require(
            json.loads((install_root / "uninstall-result.json").read_text("utf-8"))["status"]
            == "uninstalled",
            "Внешняя очистка не подтвердила uninstall.",
        )
        require(
            user_file.read_bytes() == b"preserve" and codex.is_file(),
            "Uninstall удалил пользовательский файл или изменённый Codex skill.",
        )
        require(
            not (home / ".claude/skills/language-review/SKILL.md").exists(),
            "Uninstall оставил неизменённый управляемый Claude skill.",
        )
        require(
            not (install_root / "bin/language-review.exe").exists(),
            "Uninstall оставил управляемый launcher.",
        )
        checks["uninstall_ownership"] = {"status": "PASS"}
        # Пользователь сам сохраняет/перемещает конфликтный skill до переустановки.
        codex.rename(codex.with_name("SKILL.saved.md"))
        command(bootstrap, bootstrap_env, root)
        checks["safe_reinstall"] = {"status": "PASS"}
        require(
            account_file.read_bytes() == protected_session,
            "Установка изменила синтетическую DPAPI-сессию.",
        )
        require(
            dpapi(account_file.read_bytes(), decrypt=True)
            == b"synthetic encrypted session; no real OAuth",
            "Синтетическая DPAPI-сессия не расшифровывается прежним ключом.",
        )
        require(
            user_config.read_bytes() == user_environment,
            "Установка изменила пользовательский TOML.",
        )
        checks["dpapi_environment_preserved"] = {
            "status": "PASS",
            "detail": "Синтетические данные DPAPI, без входа OAuth",
        }
        for name, value in before.items():
            require(digest(inputs / name) == value, f"Изменился внешний вход: {name}.")
        require(
            not (inputs / "repo-b/.language-review.yaml").exists(),
            "Установка создала конфигурацию во внешнем репозитории.",
        )
    return acceptance.report(manifest, digest(candidate / "release.json"), checks, predecessor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--upgrade-candidate", type=Path)
    parser.add_argument("--previous-candidate", type=Path)
    args = parser.parse_args()
    result = verify(
        args.candidate.absolute(),
        args.codex_executable.absolute() if args.codex_executable else None,
        args.upgrade_candidate.absolute() if args.upgrade_candidate else None,
        **(
            {"previous_candidate": args.previous_candidate.absolute()}
            if args.previous_candidate
            else {}
        ),
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
