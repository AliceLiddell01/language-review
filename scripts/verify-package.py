"""Собирает wheel/sdist и проверяет установленный пакет в Windows-venv."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELL_SCRIPT_SUFFIXES = (".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".bat", ".cmd")
INPUT_SAMPLE = PurePosixPath("tests/fixtures/extract/sample.ps1")
DOCUMENTATION_SUFFIXES = {".md", ".markdown", ".rst", ".adoc"}
SHELL_INTERPRETER = re.compile(
    r"(?i)(?:^|[/\\\s])(?:bash|sh|zsh|pwsh|powershell|cmd)(?:\.exe)?(?:\s|$)"
)


class SmokeFailure(RuntimeError):
    """Ошибка контракта или шага проверки собранного пакета."""


def _run(
    label: str,
    args: Sequence[str | Path],
    *,
    env: Mapping[str, str],
    expected_codes: Iterable[int] = (0,),
    stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [str(arg) for arg in args],
            cwd=PROJECT_ROOT,
            env=dict(env),
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
    except OSError as error:
        raise SmokeFailure(f"Не удалось выполнить шаг «{label}»: {error}") from error
    if stream_output:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    if result.returncode not in set(expected_codes):
        details = result.stderr or result.stdout
        for secret_name, secret_value in env.items():
            if secret_value and any(
                marker in secret_name.upper()
                for marker in ("TOKEN", "SECRET", "PASSWORD", "DSN", "KEY", "AUTH", "COOKIE")
            ):
                details = details.replace(secret_value, "<скрыто>")
        if details and not stream_output:
            print(details[-4000:].strip(), file=sys.stderr)
        raise SmokeFailure(f"Шаг «{label}» завершился с кодом {result.returncode}.")
    return result


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _json_output(output: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise SmokeFailure(f"Шаг «{label}» вернул некорректный JSON.") from error
    _require(isinstance(value, dict), f"Шаг «{label}» вернул JSON не в виде объекта.")
    return value


def _field(value: object, *keys: str) -> object:
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _assert_unchanged(path: Path, expected_hash: str, label: str) -> None:
    _require(_digest(path) == expected_hash, f"Проверка {label} изменила исходный файл.")


def _archive_files(artifact: Path) -> list[tuple[str, bytes]]:
    if artifact.name.endswith(".whl"):
        with zipfile.ZipFile(artifact) as archive:
            return [
                (member.filename, archive.read(member))
                for member in archive.infolist()
                if not member.is_dir()
            ]
    if artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            files: list[tuple[str, bytes]] = []
            for member in archive.getmembers():
                if member.isfile():
                    source = archive.extractfile(member)
                    if source is not None:
                        files.append((member.name, source.read()))
            return files
    raise SmokeFailure(f"Неизвестный формат артефакта {artifact.name}.")


def _is_input_sample(member: str) -> bool:
    normalized = PurePosixPath(member.removeprefix("./"))
    return (
        normalized == INPUT_SAMPLE
        or normalized.parts[-len(INPUT_SAMPLE.parts) :] == INPUT_SAMPLE.parts
    )


def _looks_like_shell_script(member: str, contents: bytes) -> bool:
    path = PurePosixPath(member)
    if path.suffix.lower() in DOCUMENTATION_SUFFIXES or _is_input_sample(member):
        return False
    if contents.startswith(b"MZ"):
        return False
    text = contents[:8192].decode("utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    first = lines[0]
    if first.startswith("#!"):
        return SHELL_INTERPRETER.search(first.removeprefix("#!")) is not None
    if first.lower() == "@echo off":
        return True
    lowered = "\n".join(lines).lower()
    powershell_commands = (
        "remove-item",
        "set-content",
        "get-content",
        "new-item",
        "push-location",
        "start-process",
        "write-output",
        "test-path",
        "join-path",
    )
    powershell_markers = sum(command in lowered for command in powershell_commands)
    if "$erroractionpreference" in lowered and powershell_markers:
        return True
    if re.search(r"(?im)^\s*param\s*\(", text) and powershell_markers >= 2:
        return True
    is_shell_prefix = first.lower() == "set -e" or first.lower().startswith("set -euo pipefail")
    return is_shell_prefix and any(
        token in lowered for token in ("\nthen", "\nfi", "\ncase ", "\nesac")
    )


def _assert_no_shell_outputs(artifact: Path) -> None:
    shell_files = []
    for member, contents in _archive_files(artifact):
        if _is_input_sample(member):
            continue
        if PurePosixPath(
            member
        ).suffix.lower() in SHELL_SCRIPT_SUFFIXES or _looks_like_shell_script(member, contents):
            shell_files.append(member)
    _require(
        not shell_files,
        f"Артефакт {artifact.name} содержит служебные shell-файлы: {', '.join(shell_files)}.",
    )


def _make_workspace() -> tuple[Path, Path]:
    temp_base = Path(tempfile.gettempdir()).resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="language-review-smoke-")).resolve()
    try:
        if os.path.commonpath((str(temp_base), str(temp_root))) != str(temp_base):
            raise SmokeFailure(
                "Временный путь smoke-проверки вышел за системный каталог временных файлов."
            )
    except ValueError as error:
        raise SmokeFailure("Временный путь smoke-проверки находится на другом диске.") from error
    return temp_base, temp_root


def _remove_workspace(temp_base: Path, temp_root: Path) -> None:
    resolved = temp_root.resolve()
    try:
        within_temp = os.path.commonpath((str(temp_base), str(resolved))) == str(temp_base)
    except ValueError:
        within_temp = False
    if not within_temp:
        raise SmokeFailure("Очистка smoke-проверки вышла за системный каталог временных файлов.")
    shutil.rmtree(resolved)


def _verify_installed_artifact(
    artifact: Path,
    kind: str,
    temp_root: Path,
    sample: Path,
    sample_file: Path,
    sample_hash: str,
    python: Path,
    expected_version: str,
    expected_version_output: str,
    project_env: Mapping[str, str],
) -> None:
    environment = temp_root / f"venv-{kind}"
    _run(
        "создание изолированного окружения",
        ["uv", "venv", "--python", python, environment],
        env=project_env,
        stream_output=True,
    )
    environment_python = environment / "Scripts" / "python.exe"
    _run(
        "установка пакета",
        ["uv", "pip", "install", "--python", environment_python, artifact],
        env=project_env,
        stream_output=True,
    )

    launcher = environment / "Scripts" / "language-review.exe"
    policy_path = sample / ".language-review.yaml"
    if not policy_path.is_file():
        _run("создание проектной политики", [launcher, "init", sample], env=project_env)
    policy_hash = _digest(policy_path)
    _run(
        "защита существующей проектной политики",
        [launcher, "init", sample],
        env=project_env,
        expected_codes=(2,),
    )
    _require(_digest(policy_path) == policy_hash, f"{kind} перезаписал существующую политику.")

    prompt_path = sample / "policy-prompt.md"
    prompt_path.write_text("Проверяй только доказуемые языковые ошибки.", encoding="utf-8")
    policy_text = policy_path.read_text(encoding="utf-8")
    if "primary:" not in policy_text:
        policy_path.write_text(
            policy_text.rstrip()
            + "\nprompts:\n  primary: policy-prompt.md\n"
            + "scan:\n  exclude_patterns: [policy-prompt.md]\n",
            encoding="utf-8",
        )
    policy_output = _run(
        "валидация политики",
        [launcher, "config", "validate", sample, "--format", "json"],
        env=project_env,
    ).stdout
    policy_report = _json_output(policy_output, "валидация политики")
    _require(
        _field(policy_report, "selected_profile") == "everyday"
        and isinstance(_field(policy_report, "prompts", "primary", "source"), str)
        and "policy-prompt.md" in str(_field(policy_report, "prompts", "primary", "source"))
        and "Проверяй только доказуемые" not in policy_output,
        f"{kind} потерял метаданные внешнего промпта либо раскрыл его текст.",
    )
    shown_policy = _json_output(
        _run(
            "показ конфигурации целевого файла",
            [launcher, "config", "show", sample_file, "--format", "json"],
            env=project_env,
        ).stdout,
        "показ конфигурации целевого файла",
    )
    _require(
        _field(shown_policy, "prompts", "primary", "sha256")
        == _field(policy_report, "prompts", "primary", "sha256"),
        f"{kind} не применил промпт к целевому файлу.",
    )

    version = _run(
        "версия установленного CLI", [launcher, "--version"], env=project_env
    ).stdout.strip()
    _require(
        version == expected_version_output,
        f"Установленная команда {kind} --version не прошла smoke-проверку.",
    )
    module_version = _run(
        "версия установленного модуля",
        [environment_python, "-m", "language_review", "--version"],
        env=project_env,
    ).stdout.strip()
    _require(
        module_version == expected_version_output,
        f"Установленный модуль {kind} не прошёл smoke-проверку.",
    )
    installed_version = _run(
        "метаданные установленного пакета",
        [
            environment_python,
            "-I",
            "-c",
            "import importlib.metadata as m; print(m.version('language-review'))",
        ],
        env=project_env,
    ).stdout.strip()
    _require(
        installed_version == expected_version,
        f"Метаданные установленного {kind} не совпадают с pyproject.toml.",
    )

    _run(
        "ресурсы памяти",
        [
            environment_python,
            "-I",
            "-c",
            "from importlib.resources import files; import json; "
            "p=files('language_review.memory'); "
            "assert 'CREATE EXTENSION' in p.joinpath('migrations/001.sql').read_text(); "
            "assert 'lr_jobs_finding' in p.joinpath('migrations/002.sql').read_text(); "
            "assert json.loads(p.joinpath('decision.schema.json').read_text())['type'] == 'object'",
        ],
        env=project_env,
    )
    memory_help = _run("справка CLI памяти", [launcher, "memory", "--help"], env=project_env).stdout
    _require("migrate" in memory_help, f"В {kind} недоступен CLI памяти без optional extra.")

    scan_output = _run(
        "scan установленного пакета",
        [launcher, "scan", sample, "--format", "json"],
        env=project_env,
    ).stdout
    _assert_unchanged(sample_file, sample_hash, f"{kind} scan")
    scan_report = _json_output(scan_output, "scan установленного пакета")
    _require(
        _field(scan_report, "schema_version") == "1.0"
        and _field(scan_report, "summary", "selected") == 1,
        f"Установленная команда {kind} вернула неожиданный отчёт scan.",
    )

    extract_output = _run(
        "extract установленного пакета",
        [launcher, "extract", sample, "--format", "json"],
        env=project_env,
    ).stdout
    _assert_unchanged(sample_file, sample_hash, f"{kind} extract")
    extract_report = _json_output(extract_output, "extract установленного пакета")
    documents = _field(extract_report, "documents")
    extracted_text: list[object] = []
    if isinstance(documents, list):
        for document in documents:
            if not isinstance(document, dict):
                continue
            fragments = document.get("fragments", [])
            if isinstance(fragments, list):
                extracted_text.extend(
                    fragment.get("text") for fragment in fragments if isinstance(fragment, dict)
                )
    _require(
        _field(extract_report, "schema") == "language-review.extract"
        and _field(extract_report, "summary", "extracted") == 1
        and "Пример для установленного пакета" in extracted_text,
        f"Установленная команда {kind} вернула неожиданный отчёт extract.",
    )
    module_extract = _json_output(
        _run(
            "extract установленного модуля",
            [environment_python, "-m", "language_review", "extract", sample, "--format", "json"],
            env=project_env,
        ).stdout,
        "extract установленного модуля",
    )
    _assert_unchanged(sample_file, sample_hash, f"модуль {kind} extract")
    _require(
        _field(module_extract, "schema") == "language-review.extract"
        and isinstance(_field(module_extract, "summary", "fragments"), int)
        and int(_field(module_extract, "summary", "fragments")) >= 1,
        f"Установленный модуль {kind} вернул неожиданный отчёт extract.",
    )

    vale_available = shutil.which("vale", path=project_env.get("PATH")) is not None
    check_result = _run(
        "check установленного пакета",
        [launcher, "check", sample_file, "--engine", "vale", "--format", "json"],
        env=project_env,
        expected_codes=(0, 5),
    )
    _assert_unchanged(sample_file, sample_hash, f"{kind} check")
    check_report = _json_output(check_result.stdout, "check установленного пакета")
    _require(
        _field(check_report, "schema") == "language-review.check"
        and not _field(check_report, "suggestions_applied"),
        f"Установленная команда {kind} check вернула неожиданный отчёт.",
    )
    if vale_available:
        _require(
            check_result.returncode == 0
            and _field(check_report, "status") == "clean"
            and _field(check_report, "engines", "0", "details", "configuration") == "bundled"
            and _field(check_report, "engines", "0", "status") == "ok",
            f"Установленная команда {kind} check с Vale вернула код {check_result.returncode}.",
        )
    else:
        _require(
            check_result.returncode == 5 and _field(check_report, "status") == "unavailable",
            f"Установленная команда {kind} check без Vale вернула код {check_result.returncode}.",
        )

    review_path = temp_root / f"review-{kind}.json"
    _run(
        "сохранение review",
        [
            launcher,
            "check",
            sample_file,
            "--engine",
            "vale",
            "--view",
            "review",
            "--format",
            "json",
            "--output",
            review_path,
        ],
        env=project_env,
        expected_codes=(check_result.returncode,),
    )
    review_report = _json_output(review_path.read_text(encoding="utf-8"), "сохранённый review")
    _require(
        _field(review_report, "schema") == "language-review.review"
        and _field(review_report, "schema_version") == "1.0"
        and _field(review_report, "suggestions_applied") is False
        and _field(review_report, "analysis", "schema") == "language-review.check",
        f"Review {kind} вернул неожиданный контракт.",
    )
    review_human = _run(
        "чтение сохранённого review",
        [launcher, "show-review", review_path, "--all"],
        env=project_env,
    ).stdout
    _require(
        "Языковое ревью:" in review_human, f"Show-review {kind} не прочитал сохранённый результат."
    )
    verification = _json_output(
        _run(
            "проверка сохранённого review",
            [
                environment_python,
                "-m",
                "language_review",
                "verify-review",
                review_path,
                "--root",
                f"root-0001={sample_file}",
                "--format",
                "json",
            ],
            env=project_env,
        ).stdout,
        "проверка сохранённого review",
    )
    _require(
        _field(verification, "status") == "current",
        f"Verify-review {kind} не подтвердил исходные байты.",
    )
    _assert_unchanged(sample_file, sample_hash, f"{kind} review")

    eval_launcher = environment / "Scripts" / "language-review-eval.exe"
    eval_fixture = PROJECT_ROOT / "tests" / "fixtures" / "evaluation"
    eval_corpus = temp_root / f"corpus-{kind}.json"
    eval_report_path = temp_root / f"saved-report-{kind}.json"
    shutil.copyfile(eval_fixture / "corpus.json", eval_corpus)
    shutil.copyfile(eval_fixture / "report.json", eval_report_path)
    eval_corpus_hash = _digest(eval_corpus)
    eval_report_hash = _digest(eval_report_path)
    eval_validation = _json_output(
        _run("валидация корпуса", [eval_launcher, "validate", eval_corpus], env=project_env).stdout,
        "валидация корпуса",
    )
    _require(
        _field(eval_validation, "status") == "PASS",
        f"Оценщик {kind} не прочитал упакованную схему корпуса.",
    )
    eval_result = temp_root / f"evaluation-{kind}.json"
    _run(
        "синтетическая оценка",
        [
            eval_launcher,
            "score",
            eval_corpus,
            eval_report_path,
            "--split",
            "dev",
            "--bind",
            "root-0001=demo",
            "--format",
            "json",
            "--output",
            eval_result,
        ],
        env=project_env,
        expected_codes=(4,),
    )
    scored = _json_output(eval_result.read_text(encoding="utf-8"), "синтетическая оценка")
    _require(
        _field(scored, "status") == "PARTIAL"
        and _field(scored, "report_kind") == "synthetic_contract_check"
        and _field(scored, "summary", "pipeline_detection", "tp") == 2
        and _field(scored, "summary", "pipeline_detection", "fn") == 9,
        f"Оценщик {kind} вернул неверные контрольные числа или скрыл неполноту.",
    )
    expectations = eval_fixture / "expectations.json"
    regression = _json_output(
        _run(
            "детерминированная регрессия оценки",
            [
                environment_python,
                "-m",
                "language_review.evaluation",
                "regress",
                eval_result,
                expectations,
            ],
            env=project_env,
        ).stdout,
        "детерминированная регрессия оценки",
    )
    _require(
        _field(regression, "status") == "PASS",
        f"Оценщик {kind} нарушил детерминированные ожидания.",
    )
    comparison = _json_output(
        _run(
            "сравнение результатов оценки",
            [eval_launcher, "compare", eval_result, eval_result, "--format", "json"],
            env=project_env,
        ).stdout,
        "сравнение результатов оценки",
    )
    _require(
        _field(comparison, "new_fp") == [], f"Оценщик {kind} не сравнил сохранённые результаты."
    )

    annotation_packet = temp_root / f"annotation-{kind}"
    _run(
        "экспорт слепого пакета",
        [
            eval_launcher,
            "annotation-export",
            eval_corpus,
            "--split",
            "dev",
            "--output",
            annotation_packet,
        ],
        env=project_env,
    )
    roundtrip_corpus = temp_root / f"annotation-roundtrip-{kind}.json"
    _run(
        "импорт слепого пакета",
        [
            environment_python,
            "-m",
            "language_review.evaluation",
            "annotation-import",
            eval_corpus,
            annotation_packet,
            "--output",
            roundtrip_corpus,
        ],
        env=project_env,
    )
    _require(
        json.loads(roundtrip_corpus.read_bytes()) == json.loads(eval_corpus.read_bytes()),
        f"Оценщик {kind} изменил разметку непроверенного слепого пакета.",
    )
    catalog_help = _run(
        "справка импорта источников", [eval_launcher, "import-source", "--help"], env=project_env
    ).stdout
    _require(
        "rust-book-ru" in catalog_help and "sage-github-ru" in catalog_help,
        f"Оценщик {kind} не загрузил упакованный pinned каталог.",
    )
    _require(
        _digest(eval_corpus) == eval_corpus_hash
        and _digest(eval_report_path) == eval_report_hash
        and _digest(sample_file) == sample_hash,
        f"Оценщик {kind} изменил входные данные.",
    )

    cloud_result = _run(
        "отказ без согласия на облако",
        [
            launcher,
            "check",
            sample,
            "--engine",
            "llm",
            "--llm-provider",
            "chatgpt",
            "--analysis-mode",
            "deep",
            "--view",
            "review",
            "--format",
            "json",
        ],
        env=project_env,
        expected_codes=(5,),
    )
    cloud_report = _json_output(cloud_result.stdout, "отказ без согласия на облако")
    diagnostics = _field(cloud_report, "analysis", "engines", "0", "diagnostics")
    _require(
        _field(cloud_report, "schema_version") == "1.1"
        and isinstance(diagnostics, list)
        and "cloud_consent_required" in diagnostics,
        f"{kind} потерял контракт облачного отказа.",
    )
    _assert_unchanged(sample_file, sample_hash, f"{kind} облачная проверка")

    _run(
        "отсутствие JWT в базовой установке",
        [
            environment_python,
            "-I",
            "-c",
            "import importlib.util; assert importlib.util.find_spec('jwt') is None",
        ],
        env=project_env,
    )
    _run(
        "установка chatgpt extra",
        ["uv", "pip", "install", "--python", environment_python, f"{artifact}[chatgpt]"],
        env=project_env,
        stream_output=True,
    )
    _run(
        "JWT и DPAPI",
        [
            environment_python,
            "-I",
            "-c",
            "import jwt; from language_review.chatgpt_store import dpapi; "
            "assert dpapi(dpapi(b'synthetic-test'), decrypt=True) == b'synthetic-test'",
        ],
        env=project_env,
    )
    auth_status = _json_output(
        _run("изоляция OAuth-хранилища", [launcher, "chatgpt", "status"], env=project_env).stdout,
        "изоляция OAuth-хранилища",
    )
    _require(
        _field(auth_status, "active") is None and _field(auth_status, "accounts") == [],
        f"{kind} не изолировал хранилище OAuth при проверке пакета "
        "(ожидались active=null и пустой список accounts).",
    )
    _run(
        "установка memory extra",
        ["uv", "pip", "install", "--python", environment_python, f"{artifact}[memory]"],
        env=project_env,
        stream_output=True,
    )
    _run(
        "optional memory extra",
        [
            environment_python,
            "-I",
            "-c",
            "import psycopg; from language_review.memory.store import Store; "
            "from language_review.memory.settings import MemorySettings; "
            "assert Store(MemorySettings()).settings.enabled is False",
        ],
        env=project_env,
    )

    memory_dsn = project_env.get("LANGUAGE_REVIEW_MEMORY_TEST_DSN")
    if memory_dsn:
        memory_config = temp_root / f"memory-{kind}.toml"
        memory_config.write_text(
            '[memory]\ndsn_env="LANGUAGE_REVIEW_MEMORY_TEST_DSN"\n', encoding="utf-8"
        )
        _run(
            "health установленного пакета с тестовой БД",
            [launcher, "--config", memory_config, "memory", "health"],
            env=project_env,
            stream_output=True,
        )


def verify_package() -> None:
    python = Path(
        _run("поиск Python 3.15", ["uv", "python", "find", "3.15"], env=os.environ).stdout.strip()
    )
    _require(python.is_file(), "Не удалось найти Python 3.15 через uv.")
    expected_version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))[
        "project"
    ]["version"]
    expected_version_output = f"language-review {expected_version}"

    temp_base, temp_root = _make_workspace()
    project_env = os.environ.copy()
    project_env["APPDATA"] = str(temp_root / "appdata")
    project_env["LOCALAPPDATA"] = str(temp_root / "localappdata")
    project_env["PYTHONIOENCODING"] = "utf-8"
    (temp_root / "appdata").mkdir()
    (temp_root / "localappdata").mkdir()
    if not project_env.get("UV_CACHE_DIR"):
        project_env["UV_CACHE_DIR"] = _run(
            "определение кэша uv", ["uv", "cache", "dir"], env=project_env
        ).stdout.strip()

    try:
        distribution_directory = temp_root / "dist"
        distribution_directory.mkdir()
        _run(
            "сборка wheel/sdist",
            ["uv", "build", "--wheel", "--sdist", "--out-dir", distribution_directory],
            env=project_env,
            stream_output=True,
        )
        artifacts = sorted(
            path
            for path in distribution_directory.iterdir()
            if path.is_file() and path.name.startswith(f"language_review-{expected_version}")
        )
        _require(len(artifacts) == 2, f"Ожидались wheel и sdist, найдено: {len(artifacts)}.")
        kinds = {
            "wheel": [path for path in artifacts if path.name.endswith(".whl")],
            "sdist": [path for path in artifacts if path.name.endswith(".tar.gz")],
        }
        _require(
            all(len(paths) == 1 for paths in kinds.values()),
            "Сборка не создала ровно один wheel и один sdist.",
        )
        for artifact in artifacts:
            _assert_no_shell_outputs(artifact)

        sample = temp_root / "пример"
        sample.mkdir()
        sample_file = sample / "Пример.rs"
        sample_file.write_text(
            'fn main() { println!("Пример для установленного пакета"); }',
            encoding="utf-8",
            newline="",
        )
        sample_hash = _digest(sample_file)
        for kind, paths in kinds.items():
            artifact = paths[0]
            _verify_installed_artifact(
                artifact,
                kind,
                temp_root,
                sample,
                sample_file,
                sample_hash,
                python,
                expected_version,
                expected_version_output,
                project_env,
            )
            print(f"{kind}: установка, CLI, python -m, scan, extract, check, review и eval прошли.")
    finally:
        _remove_workspace(temp_base, temp_root)


def main() -> int:
    try:
        verify_package()
    except (SmokeFailure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Ошибка проверки пакета: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
