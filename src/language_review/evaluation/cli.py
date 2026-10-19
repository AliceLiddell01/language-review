"""PowerShell-friendly entry point независимого оценщика."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from language_review.config import CHECK_CATEGORIES, CHECK_ENGINES
from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.evaluation.annotation_io import export_annotations, import_annotations
from language_review.evaluation.comparison import (
    check_expectation,
    compare,
    distribution,
    expectation,
)
from language_review.evaluation.contracts import canonical, digest, read_json, save_new, validate
from language_review.evaluation.corpus import (
    annotation_agreement,
    load_corpus,
    test_hash,
    validate_corpus,
)
from language_review.evaluation.live import capture, materialize
from language_review.evaluation.reference import reference_score
from language_review.evaluation.reports import load_product_report
from language_review.evaluation.scoring import score
from language_review.evaluation.source_import import import_source, recipes
from language_review.review_report import _quote
from language_review.scanner import _classify_encoding


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="language-review-eval",
        description="Независимая оценка сохранённых check/review без изменения исходников",
    )
    commands = root.add_subparsers(dest="command", required=True)
    source = commands.add_parser(
        "import-source", help="получить pinned разрешённый реальный источник"
    )
    source.add_argument("source", choices=tuple(recipes()))
    source.add_argument("--output", type=Path, required=True, help="новый внешний каталог")
    source.add_argument("--limit", type=int, default=250)
    source.add_argument("--cache", type=Path, help="исходные файлы с теми же pinned hashes")
    source.add_argument(
        "--pair-format",
        choices=("plain", "markdown"),
        default="plain",
        help="адаптер source-поля; Markdown только для сравнения старого пилота",
    )
    reference = commands.add_parser(
        "reference-score", help="совпадение с pinned опубликованными correction; не gold metrics"
    )
    reference.add_argument("corpus", type=Path)
    reference.add_argument("report", type=Path)
    reference.add_argument("--source-bundle", type=Path, required=True)
    reference.add_argument("--run-manifest", type=Path, required=True)
    reference.add_argument("--split", choices=("dev", "validation", "test"), required=True)
    reference.add_argument("--output", type=Path)
    for name in ("annotation-export", "annotation-import"):
        annotation = commands.add_parser(name, help="слепая разметка JSONL/CSV")
        annotation.add_argument("corpus", type=Path)
        annotation.add_argument("--output", type=Path, required=True)
        if name == "annotation-export":
            annotation.add_argument("--split", choices=("dev", "validation", "test"), required=True)
        else:
            annotation.add_argument("packet", type=Path)
            annotation.add_argument("--actor", default="")
            annotation.add_argument("--evidence", default="")
            annotation.add_argument(
                "--attest-human",
                action="store_true",
                help="реальный человек подтверждает собственную независимую разметку",
            )
    for name, help_text in (
        ("validate", "проверить корпус"),
        ("export", "канонический импорт/экспорт корпуса"),
        ("freeze-test", "зафиксировать test split"),
        ("materialize", "экспортировать исходники split без меток"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("corpus", type=Path)
        if name != "validate":
            command.add_argument("--output", type=Path, required=True)
        if name == "materialize":
            command.add_argument("--split", choices=("dev", "validation", "test"), required=True)
    init = commands.add_parser("init", help="создать черновик корпуса из разрешённого файла")
    init.add_argument("source", type=Path)
    for name in ("repository", "path", "origin", "rights-basis"):
        init.add_argument("--" + name, required=True)
    init.add_argument("--data-kind", choices=("real", "synthetic"), required=True)
    init.add_argument("--publish", choices=("permitted", "private_only"), default="private_only")
    init.add_argument("--output", type=Path, required=True)
    score_parser = commands.add_parser("score", help="оценить настоящий сохранённый отчёт offline")
    score_parser.add_argument("corpus", type=Path)
    score_parser.add_argument("report", type=Path)
    score_parser.add_argument("--split", choices=("dev", "validation", "test"), required=True)
    score_parser.add_argument("--bind", action="append", default=[], metavar="ROOT=REPOSITORY")
    score_parser.add_argument("--run-manifest", type=Path)
    score_parser.add_argument("--judgments", type=Path, help="независимые ручные решения")
    score_parser.add_argument("--output", type=Path)
    score_parser.add_argument("--format", choices=("human", "json"), default="human")
    diff = commands.add_parser("compare", help="предметно сравнить совместимые оценки")
    diff.add_argument("baseline", type=Path)
    diff.add_argument("candidate", type=Path)
    diff.add_argument("--format", choices=("human", "json"), default="human")
    diff.add_argument("--output", type=Path)
    regression = commands.add_parser("regress", help="проверить детерминированные ожидания offline")
    regression.add_argument("result", type=Path)
    regression.add_argument("expectations", type=Path)
    regression.add_argument("--record", action="store_true", help="создать новый файл ожиданий")
    agreement = commands.add_parser(
        "agreement", help="сверить независимые аннотации; не назначает истину"
    )
    agreement.add_argument("first", type=Path)
    agreement.add_argument("second", type=Path)
    aggregate = commands.add_parser("aggregate", help="описать распределение повторных запусков")
    aggregate.add_argument("results", type=Path, nargs="+")
    run = commands.add_parser("run", help="явно запустить публичный check и тот же offline scorer")
    run.add_argument("corpus", type=Path)
    run.add_argument("--split", choices=("dev", "validation", "test"), required=True)
    run.add_argument("--engine", action="append", choices=CHECK_ENGINES, required=True)
    run.add_argument("--config", type=Path)
    run.add_argument("--product-commit", required=True, help="точный SHA установленной программы")
    run.add_argument("--output", type=Path, required=True, help="новый внешний каталог артефактов")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--timeout", type=float, default=600)
    return root


def render(result: dict[str, Any]) -> str:
    if result.get("schema") == "language-review.evaluation":
        summary = result["summary"]
        lines = [
            f"Оценка: {result['status']} · {result['report_kind']}",
            f"Split: {result['compatibility']['split']}; "
            f"корпус: {result['compatibility']['corpus_sha256']}",
            f"Случаев: {summary['cases']}; "
            f"оценено применимых: {summary['evaluated_applicable_cases']}; "
            f"неполных: {summary['incomplete_cases']}; "
            f"не оценено: {summary['not_evaluated_cases']}",
            "Уровень | TP | FP | FN | Precision | Recall | F1 | F0.5",
            "--- | --- | --- | --- | --- | --- | --- | ---",
        ]
        for label in ("detection", "pipeline_detection", "correction"):
            metric = summary[label]
            values = [
                str(metric[key]) if metric[key] is not None else "N/A (нулевой знаменатель)"
                for key in ("tp", "fp", "fn", "precision", "recall", "f1", "f0_5")
            ]
            lines.append(label + " | " + " | ".join(values))
        lines += [
            f"TN чистых случаев: {summary['tn_clean_cases']}; "
            "FP/1000 оценённых применимых случаев: "
            f"{summary['fp_per_1000_evaluated_applicable_cases']}",
            "Статусы разметки: "
            + json.dumps(result["diagnostics"]["label_states"], ensure_ascii=False),
        ]
        for dimension, groups in result["strata"].items():
            lines += [
                f"Разрез {dimension}: n — число случаев, не оценка репрезентативности.",
                "Группа | n | TP | FP | FN",
                "--- | --- | --- | --- | ---",
            ]
            for key, group in groups.items():
                metric = group["pipeline_detection"]
                lines.append(
                    f"{_quote(key)} | {group['cases']} | "
                    f"{metric['tp']} | {metric['fp']} | {metric['fn']}"
                )
        for engine, value in result["engines"].items():
            metric = value["summary"]["pipeline_detection"]
            lines.append(
                f"Движок {engine}: TP={metric['tp']} FP={metric['fp']} FN={metric['fn']}; "
                "вклад " + json.dumps(result["contribution"][engine], ensure_ascii=False)
            )
        for case in result["cases"]:
            lines.append(
                f"{_quote(case['id'])}: {case['state']}; TP={case['tp']}; FN={case['fn']}; "
                f"причины={case['reasons']}"
            )
        lines.append("Доступные затраты: " + json.dumps(result["cost"], ensure_ascii=False))
        lines.append("Ограничения: " + "; ".join(result["diagnostics"]["limitations"]))
        return "\n".join(lines)
    if result.get("schema") == "language-review.evaluation-comparison":
        return "Сравнение оценок\n" + "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in result.items()
            if key not in {"schema", "schema_version", "compatibility"}
        )
    return canonical(result).rstrip()


def _emit(result: dict[str, Any], args: argparse.Namespace) -> None:
    content = (
        canonical(result) if getattr(args, "format", "json") == "json" else render(result) + "\n"
    )
    target = getattr(args, "output", None)
    if target:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    else:
        print(content, end="")


def _init(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.source.read_bytes()
    encoding, bom, _, reason = _classify_encoding(raw)
    if reason or not encoding:
        raise InputError("Исходник не поддерживается текущим scan.")
    source = SourceMap.create(raw, encoding, bom)
    annotation = {
        "status": "draft",
        "actor": "unassigned",
        "method": "human",
        "evidence": "",
        "history": [],
    }
    return validate_corpus(
        {
            "schema": "language-review.corpus",
            "schema_version": "1.0",
            "id": "new-corpus",
            "version": "1",
            "annotation_version": "1",
            "split_policy": {
                "strategy": "repository grouping; normalized 5-character shingles "
                "Jaccard >= 0.8 and containment; manual near-duplicate audit required",
                "test_frozen": False,
                "test_sha256": None,
                "holdout_policy": "answers restricted to independent evaluation; "
                "do not use in prompts/retrieval",
            },
            "documents": [
                {
                    "id": "source-1",
                    "repository": args.repository,
                    "path": args.path,
                    "revision": "unassigned",
                    "origin": args.origin,
                    "rights": {
                        "use": "permitted",
                        "publish": args.publish,
                        "basis": args.rights_basis,
                    },
                    "data_kind": args.data_kind,
                    "content_base64": base64.b64encode(raw).decode("ascii"),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "encoding": encoding,
                    "bom": bom,
                }
            ],
            "cases": [
                {
                    "id": "case-1",
                    "document": "source-1",
                    "split": "dev",
                    "range": source.range_dict(0, len(source.text)),
                    "context": "other",
                    "profile": None,
                    "languages": ["ru"],
                    "categories": list(CHECK_CATEGORIES),
                    "terms": [],
                    "exceptions": [],
                    "protected_ranges": [],
                    "applicable": True,
                    "exhaustive": False,
                    "verdict": "disputed",
                    "annotation": annotation,
                    "labels": [],
                }
            ],
        }
    )


def _run(args: argparse.Namespace) -> int:
    if args.command == "import-source":
        print(
            canonical(
                import_source(args.source, args.output, args.limit, args.cache, args.pair_format)
            ),
            end="",
        )
        return 0
    if args.command == "reference-score":
        result = reference_score(
            load_corpus(args.corpus),
            load_product_report(args.report),
            args.source_bundle,
            args.split,
            read_json(args.run_manifest),
        )
        _emit(result, args)
        return 0 if result["scopes"]["pipeline"]["summary"]["status"] == "PASS" else 4
    if args.command in {"annotation-export", "annotation-import"}:
        corpus = load_corpus(args.corpus)
        if args.command == "annotation-export":
            export_annotations(corpus, args.split, args.output)
        else:
            save_new(
                args.output,
                import_annotations(
                    corpus, args.packet, args.actor, args.evidence, args.attest_human
                ),
            )
        return 0
    if args.command == "init":
        save_new(args.output, _init(args))
        return 0
    if args.command in {"validate", "export", "freeze-test", "materialize", "score", "run"}:
        corpus = load_corpus(args.corpus)
        if args.command == "validate":
            print(
                canonical(
                    {
                        "status": "PASS",
                        "corpus_sha256": digest(corpus),
                        "cases": len(corpus["cases"]),
                    }
                ),
                end="",
            )
            return 0
        if args.command == "export":
            save_new(args.output, corpus)
            return 0
        if args.command == "freeze-test":
            corpus["split_policy"].update({"test_frozen": True, "test_sha256": test_hash(corpus)})
            save_new(args.output, validate_corpus(corpus))
            return 0
        if args.command == "materialize":
            print(canonical(materialize(corpus, args.split, args.output)), end="")
            return 0
        if args.command == "run":
            if not re.fullmatch(r"[a-f0-9]{40}", args.product_commit):
                raise InputError("--product-commit требует полный SHA из 40 символов.")
            result = capture(
                corpus,
                args.split,
                list(dict.fromkeys(args.engine)),
                args.config,
                args.output,
                args.product_commit,
                args.repeat,
                args.timeout,
            )
            print(canonical(result), end="")
            return 0 if result["status"] == "PASS" else 4
        bindings = {}
        for value in args.bind:
            key, separator, repository = value.partition("=")
            if not separator or not key or not repository or key in bindings:
                raise InputError("--bind требует уникальные ROOT=REPOSITORY.")
            bindings[key] = repository
        report = load_product_report(args.report)
        manifest = read_json(args.run_manifest) if args.run_manifest else None
        if manifest:
            validate(manifest, "run")
            if bindings and bindings != manifest["root_bindings"]:
                raise InputError("--bind противоречит манифесту.")
            bindings = manifest["root_bindings"]
        judgments = read_json(args.judgments) if args.judgments else None
        result = score(corpus, report, bindings, args.split, manifest, judgments)
        _emit(result, args)
        return 0 if result["status"] == "PASS" else 4
    if args.command == "compare":
        _emit(compare(read_json(args.baseline), read_json(args.candidate)), args)
        return 0
    if args.command == "regress":
        result = read_json(args.result)
        if args.record:
            save_new(args.expectations, expectation(result))
            return 0
        verification = check_expectation(result, read_json(args.expectations))
        print(canonical(verification), end="")
        return 0 if verification["status"] == "PASS" else 1
    if args.command == "agreement":
        print(
            canonical(annotation_agreement(load_corpus(args.first), load_corpus(args.second))),
            end="",
        )
        return 0
    if args.command == "aggregate":
        print(canonical(distribution([read_json(path) for path in args.results])), end="")
        return 0
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return _run(args)
    except InputError as exc:
        print(f"Ошибка оценки: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "Ошибка оценки: недоступный файл или ошибка записи; "
            "существующие артефакты не перезаписываются.",
            file=sys.stderr,
        )
        return 3
    except ValueError, LookupError, TypeError, RecursionError, KeyError:
        print("Ошибка оценки: некорректные данные или контракт.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
