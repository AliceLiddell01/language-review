"""Настройка среды пользователя без создания проектной языковой политики."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from language_review import distribution as deployment
from language_review.config import load_config, user_config_path
from language_review.errors import ConfigurationError
from language_review.release_contract import ReleaseError


def add_commands(commands: Any) -> None:
    setup = commands.add_parser("setup", help="повторить пользовательскую настройку окружения")
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--codex", action=argparse.BooleanOptionalAction, default=None)
    setup.add_argument("--claude", action=argparse.BooleanOptionalAction, default=None)
    for service in ("ollama", "llama-cpp", "languagetool"):
        setup.add_argument("--" + service + "-endpoint")
    setup.add_argument(
        "--chatgpt-login", action="store_true", help="явно начать OAuth; не выдаёт cloud consent"
    )
    setup.add_argument(
        "--memory-service", action="store_true", help="явно установить/обновить существующий worker"
    )
    setup.add_argument("--memory-runtime", type=Path)
    setup.add_argument("--memory-project", default="language-review-memory")
    setup.add_argument("--memory-llm-model")
    setup.add_argument("--memory-embedding-model")
    setup.add_argument("--memory-embedding-revision")
    setup.add_argument("--memory-embedding-dimensions", type=int)
    install = commands.add_parser(
        "installation", help="версия, доступные релизы, обновление и удаление"
    )
    operations = install.add_subparsers(dest="installation_command", required=True)
    operations.add_parser("status", help="проверить CLI и drift skills без сети")
    available = operations.add_parser(
        "available", help="узнать опубликованную версию без установки"
    )
    available.add_argument("--prerelease", action="store_true")
    update = operations.add_parser("update", help="явно установить выбранный опубликованный релиз")
    update.add_argument("--tag")
    update.add_argument("--prerelease", action="store_true")
    update.add_argument("--allow-downgrade", action="store_true")
    update.add_argument("--extras", choices=("core", "chatgpt", "memory", "all"))
    operations.add_parser(
        "rollback", help="восстановить предыдущий CLI; БД остаётся у владельца worker"
    )
    operations.add_parser(
        "uninstall", help="удалить launcher/skills; оставить данные и runtime до внешней очистки"
    )


def configure_endpoints(values: dict[str, str]) -> None:
    if not values:
        return
    target = user_config_path()
    deployment.safe_path(target)
    begin, end = "# language-review setup: начало", "# language-review setup: конец"
    text = target.read_text("utf-8") if target.exists() else ""
    if text and (begin not in text or end not in text):
        raise ReleaseError(
            "Пользовательский TOML создан вручную; настройте endpoints в нём. "
            "Setup его не перезаписывает."
        )
    previous: dict[str, str] = {}
    if text:
        import tomllib

        parsed = tomllib.loads(text)
        for service, table in (
            ("ollama", ["check", "llm", "ollama"]),
            ("llama_cpp", ["check", "llm", "llama_cpp"]),
            ("languagetool", ["check", "languagetool"]),
        ):
            node = parsed
            for key in table:
                node = node.get(key, {})
            if "endpoint" in node:
                previous[service] = node["endpoint"]
    previous.update(values)
    lines = [begin]
    for service, value in previous.items():
        parsed_url = urllib.parse.urlsplit(value)
        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ReleaseError(
                "Setup принимает локальные HTTP endpoints без credentials; "
                "удалённые требуют отдельной ручной настройки согласий."
            )
        section = "check.languagetool" if service == "languagetool" else "check.llm." + service
        lines.extend(["[" + section + "]", "endpoint = " + json.dumps(value), ""])
    block = "\n".join([*lines, end, ""])
    if text:
        before, rest = text.split(begin, 1)
        _, after = rest.split(end, 1)
        block = before + block + after
    with tempfile.TemporaryDirectory(prefix="language-review-config-") as directory:
        candidate = Path(directory) / "candidate.toml"
        candidate.write_text(block, encoding="utf-8")
        load_config(candidate)
    deployment.atomic(target, block.encode("utf-8"))


def setup(arguments: argparse.Namespace) -> int:
    root = deployment.managed_root()
    current = deployment.state(root)
    if not arguments.non_interactive:
        for name, prompt in (
            ("codex", "Глобальный skill Codex? "),
            ("claude", "Опциональный skill Claude Code? "),
        ):
            if getattr(arguments, name) is None:
                installed = name in current.get("skills", {})
                answer = input(prompt + ("[Д/н] " if installed else "[д/Н] ")).strip().casefold()
                setattr(
                    arguments,
                    name,
                    installed if not answer else answer in {"д", "да", "y", "yes"},
                )
        for name, prompt in (
            ("ollama_endpoint", "Локальный endpoint Ollama (Enter — оставить): "),
            ("llama_cpp_endpoint", "Локальный endpoint llama.cpp (Enter — оставить): "),
            ("languagetool_endpoint", "Локальный endpoint LanguageTool (Enter — оставить): "),
        ):
            if getattr(arguments, name) is None:
                setattr(arguments, name, input(prompt).strip() or None)
        print("ChatGPT: setup --chatgpt-login; OAuth не выдаёт согласие на отправку текстов.")
        print(
            "Память: setup --memory-service с явно выбранными моделями. "
            "Docker/модели автоматически не устанавливаются."
        )
        if current["extras"] in {"chatgpt", "all"} and not arguments.chatgpt_login:
            arguments.chatgpt_login = input(
                "Начать ChatGPT OAuth без выдачи cloud consent? [д/Н] "
            ).strip().casefold() in {"д", "да", "y", "yes"}
        if current["extras"] in {"memory", "all"} and not arguments.memory_service:
            arguments.memory_service = input(
                "Настроить memory-worker с уже установленными Docker/Ollama/моделями? [д/Н] "
            ).strip().casefold() in {"д", "да", "y", "yes"}
        if arguments.memory_service:
            for name, prompt in (
                ("memory_llm_model", "Имя установленной локальной LLM: "),
                ("memory_embedding_model", "Имя embedding-модели: "),
                ("memory_embedding_revision", "Точный SHA-256 embedding-модели: "),
            ):
                if getattr(arguments, name) is None:
                    setattr(arguments, name, input(prompt).strip())
            if arguments.memory_embedding_dimensions is None:
                try:
                    arguments.memory_embedding_dimensions = int(input("Размерность embedding: "))
                except ValueError as exc:
                    raise ReleaseError("Размерность должна быть целым числом.") from exc
    selected = set(current["skills"])
    for agent in ("codex", "claude"):
        value = getattr(arguments, agent)
        if value is True:
            selected.add(agent)
        elif value is False:
            selected.discard(agent)
    endpoints = {
        name: value
        for name in ("ollama", "llama_cpp", "languagetool")
        if (value := getattr(arguments, name + "_endpoint")) is not None
    }
    configure_endpoints(endpoints)
    deployment.setup_skills(root, sorted(selected))
    if arguments.chatgpt_login:
        if current["extras"] not in {"chatgpt", "all"}:
            raise ReleaseError(
                "OAuth требует extras chatgpt/all; явно обновите выбранную установку."
            )
        from language_review.chatgpt_cli import run as chatgpt

        code = chatgpt(
            argparse.Namespace(chatgpt_command="login", timeout=300, account=None, consent=False)
        )
        if code:
            return code
    if arguments.memory_service:
        if current["extras"] not in {"memory", "all"}:
            raise ReleaseError("Worker требует extras memory/all.")
        if not all(
            (
                arguments.memory_llm_model,
                arguments.memory_embedding_model,
                arguments.memory_embedding_revision,
                arguments.memory_embedding_dimensions,
            )
        ):
            raise ReleaseError(
                "Для worker явно укажите model, embedding model/revision/dimensions; "
                "шаблон проекта не создаётся."
            )
        bundle = root / "releases" / current["active"] / "bundle"
        from language_review.release_contract import validate_bundle

        validate_bundle(bundle)
        command = [
            "pwsh",
            "-NoProfile",
            "-File",
            str(bundle / "install-memory-service.ps1"),
            "-ReleaseDirectory",
            str(bundle),
            "-Project",
            arguments.memory_project,
            "-LlmModel",
            arguments.memory_llm_model,
            "-EmbeddingModel",
            arguments.memory_embedding_model,
            "-EmbeddingRevision",
            arguments.memory_embedding_revision,
            "-EmbeddingDimensions",
            str(arguments.memory_embedding_dimensions),
        ]
        if arguments.memory_runtime:
            command += ["-RuntimeDirectory", str(arguments.memory_runtime)]
        import os

        environment = dict(os.environ)
        environment["PATH"] = str(Path(current["uv"]).parent) + os.pathsep + environment["PATH"]
        result = subprocess.run(command, check=False, env=environment)
        if result.returncode:
            raise ReleaseError(
                "Worker не готов; CLI установлен. "
                "Прежний worker/БД проверяются владельцем deployment."
            )
    print("Ядро настроено. Проект требует отдельного language-review init по вашей команде.")
    print("Codex/Claude: перезапустите сессию после изменения глобальных skills.")
    from language_review.doctor import doctor_human, run_doctor

    print(doctor_human(run_doctor(load_config(), [])))
    return 0


def run(arguments: argparse.Namespace) -> int:
    try:
        if arguments.command == "setup":
            return setup(arguments)
        from language_review.release_transport import fetch_bundle, published

        operation = arguments.installation_command
        if operation == "available":
            release = published(prerelease=arguments.prerelease)
            print(
                json.dumps(
                    {
                        "tag": release["tag_name"],
                        "prerelease": release["prerelease"],
                        "url": release["html_url"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        root = deployment.managed_root()
        if operation == "status":
            result = deployment.status(root)
        elif operation == "rollback":
            result = deployment.rollback(root)
        elif operation == "uninstall":
            preserved = deployment.uninstall(root)
            deployment.schedule_cleanup(root)
            result = {
                "status": "uninstalled",
                "preserved": preserved,
                "data": "OAuth, TOML, memory runtime/БД и backups сохранены; результаты очистки: "
                + str(root),
            }
        else:
            current = deployment.state(root)
            with tempfile.TemporaryDirectory(prefix="language-review-update-") as directory:
                bundle = Path(directory)
                fetch_bundle(bundle, arguments.tag, prerelease=arguments.prerelease)
                uv = shutil.which("uv") or current["uv"]
                result = deployment.install(
                    bundle,
                    root,
                    uv=uv,
                    extras=arguments.extras or current["extras"],
                    home=Path(current["home"]),
                    allow_downgrade=arguments.allow_downgrade,
                )
            result = {
                "status": "updated",
                "version": result["version"],
                "worker": "Сохранён отдельный runtime worker; обновление: setup --memory-service.",
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ReleaseError, ConfigurationError) as exc:
        print("Настройка/установка: " + str(exc), file=__import__("sys").stderr)
        return 2
