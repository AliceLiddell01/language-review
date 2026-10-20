from __future__ import annotations

import argparse
import json
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from language_review.config import LoadedConfig
from language_review.errors import InputError
from language_review.memory.pipeline import import_review
from language_review.memory.settings import project_uuid
from language_review.memory.store import MemoryError, Store


def add_commands(commands: Any, check: argparse.ArgumentParser) -> None:
    check.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="явно включить или отключить предметную память",
    )
    check.add_argument(
        "--memory-project", metavar="UUID", help="постоянная идентичность одного корня"
    )
    check.add_argument(
        "--memory-run", metavar="UUID", help="ключ повторной доставки одного анализа"
    )
    check.add_argument("--memory-mode", choices=("structured", "hybrid"))
    check.add_argument(
        "--memory-dataset",
        default=None,
        choices=("operational", "synthetic", "train", "dev", "test", "holdout"),
    )
    decision = commands.add_parser("decide-review", help="зарегистрировать содержательное решение")
    decision.add_argument("decision", type=Path, help="JSON language-review.decision 1.0")
    memory = commands.add_parser("memory", help="эксплуатация предметной памяти")
    actions = memory.add_subparsers(dest="memory_command", required=True)
    actions.add_parser("migrate", help="применить проверяемые миграции")
    actions.add_parser("health", help="проверить схему и pgvector")
    for name, help_text in (
        ("status", "счётчики, очередь, ошибки и индекс проекта"),
        ("run", "показать записанный прогон и корреляцию item"),
        ("finding", "свидетельства и история решения одного случая"),
        ("search", "поиск свидетельств с достоверностью"),
        ("work", "обработать очередь LLM и проиндексировать"),
        ("reindex", "перестроить производные векторы"),
        ("retry", "явно повторить исчерпавшие попытки задания"),
    ):
        command = actions.add_parser(name, help=help_text)
        if name == "work":
            scope = command.add_mutually_exclusive_group(required=True)
            scope.add_argument("--project", metavar="UUID")
            scope.add_argument(
                "--all-projects", action="store_true", help="оператор всей очереди этой БД"
            )
        else:
            command.add_argument("--project", required=True, metavar="UUID")
        if name == "run":
            command.add_argument("run_id")
        if name == "finding":
            command.add_argument("finding_id")
        if name == "search":
            command.add_argument("query")
            command.add_argument("--structured", action="store_true")
            command.add_argument("--include-hypotheses", action="store_true")
            command.add_argument("--kind")
            command.add_argument(
                "--dataset",
                default="operational",
                choices=("operational", "synthetic", "train", "dev"),
            )
        if name in {"work", "reindex"}:
            command.add_argument("--limit", type=int, default=32)
        if name == "work":
            command.add_argument("--watch", action="store_true", help="постоянный bounded worker")
    historical = actions.add_parser("import", help="идемпотентно импортировать проверенный review")
    historical.add_argument("report", type=Path)
    historical.add_argument("--root", action="append", default=[], metavar="ID=ПУТЬ")
    historical.add_argument("--project", action="append", default=[], metavar="ID=UUID")
    historical.add_argument(
        "--dataset",
        required=True,
        choices=("operational", "synthetic", "train", "dev", "test", "holdout"),
    )


def pairs(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        key, separator, target = value.partition("=")
        if not separator or not target or key in result:
            raise InputError("Требуются уникальные пары ID=ЗНАЧЕНИЕ.")
        result[key] = target
    return result


def submit_decision(store: Store, document: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads(
        files("language_review.memory").joinpath("decision.schema.json").read_text("utf-8")
    )
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except ValidationError as exc:
        raise InputError("Некорректный контракт решения.") from exc
    return store.decide(
        document["project_id"], document["run_id"], document["item_id"], document["decision"]
    )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def run(arguments: argparse.Namespace, config: LoadedConfig) -> int:
    store = Store(config.memory)
    if arguments.command == "decide-review":
        try:
            document = json.loads(arguments.decision.read_text("utf-8"), object_pairs_hook=_unique)
        except (OSError, ValueError) as exc:
            raise InputError("Недоступный или некорректный JSON решения.") from exc
        result = submit_decision(store, document)
    else:
        operation = arguments.memory_command
        if operation == "migrate":
            result = store.migrate()
        elif operation == "health":
            result = store.health()
        elif operation == "import":
            result = import_review(
                store,
                arguments.report,
                {key: Path(value) for key, value in pairs(arguments.root).items()},
                pairs(arguments.project),
                arguments.dataset,
            )
        else:
            project = project_uuid(arguments.project) if arguments.project else ""
            store.health()
            if operation == "status":
                result = store.status(project)
            elif operation == "finding":
                with store.connect() as db:
                    finding = db.execute(
                        "SELECT * FROM lr_findings WHERE id=%s AND project_id=%s",
                        (arguments.finding_id, project),
                    ).fetchone()
                    if finding is None:
                        raise MemoryError("memory_finding_not_in_project")
                    decisions = db.execute(
                        "SELECT * FROM lr_decisions WHERE finding_id=%s ORDER BY revision",
                        (arguments.finding_id,),
                    ).fetchall()
                    occurrences = db.execute(
                        "SELECT run_id,item_id FROM lr_occurrences "
                        "WHERE finding_id=%s ORDER BY run_id",
                        (arguments.finding_id,),
                    ).fetchall()
                    history = db.execute(
                        "SELECT h.* FROM lr_rule_history h JOIN lr_rules r "
                        "ON r.id=h.rule_id WHERE r.finding_id=%s "
                        "ORDER BY h.rule_id,h.revision",
                        (arguments.finding_id,),
                    ).fetchall()
                result = {
                    "finding": finding,
                    "decisions": decisions,
                    "occurrences": occurrences,
                    "rule_history": history,
                }
            elif operation == "run":
                with store.connect() as db:
                    row = db.execute(
                        "SELECT id,status,dataset,provenance,diagnostics FROM lr_runs "
                        "WHERE id=%s AND EXISTS (SELECT 1 FROM jsonb_each_text(bindings) b "
                        "WHERE b.value=%s)",
                        (arguments.run_id, project),
                    ).fetchone()
                    if row is None:
                        raise MemoryError("memory_run_not_in_project")
                    occurrences = db.execute(
                        "SELECT o.item_id,o.finding_id,f.state,f.revision "
                        "FROM lr_occurrences o JOIN lr_findings f ON f.id=o.finding_id "
                        "WHERE o.run_id=%s AND f.project_id=%s ORDER BY o.item_id",
                        (arguments.run_id, project),
                    ).fetchall()
                    document_ids = {
                        doc["id"]
                        for doc in db.execute(
                            "SELECT id FROM lr_documents WHERE run_id=%s AND project_id=%s",
                            (arguments.run_id, project),
                        ).fetchall()
                    }
                    diagnostics = row["diagnostics"]
                    if "queries" in diagnostics:
                        diagnostics["queries"] = [
                            query
                            for query in diagnostics["queries"]
                            if query.get("document_id") in document_ids
                        ]
                        diagnostics["context_units"] = sum(
                            bool(query["rules"]) for query in diagnostics["queries"]
                        )
                        diagnostics["policy_sha256"] = {
                            key: value
                            for key, value in diagnostics.get("policy_sha256", {}).items()
                            if key in document_ids
                        }
                result = {**dict(row), "items": occurrences}
            elif operation == "retry":
                result = {"retried": store.retry(project)}
            elif operation == "search":
                from language_review.memory.search import recall

                result = recall(
                    store,
                    project,
                    arguments.query,
                    kind=arguments.kind,
                    dataset=arguments.dataset,
                    hybrid=not arguments.structured,
                    include_hypotheses=arguments.include_hypotheses,
                )
            elif operation == "reindex":
                from language_review.memory.search import index_rules

                result = index_rules(store, project, limit=arguments.limit, rebuild=True)
            else:
                from language_review.memory.worker import work

                while True:
                    result = work(store, project or None, config.check, limit=arguments.limit)
                    if not arguments.watch:
                        break
                    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
                    time.sleep(2)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 4 if result.get("status") == "partial" else 0
