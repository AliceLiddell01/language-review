"""Воспроизводимый замер `language-review check` на внешнем корпусе AzurPilotRu.

Скрипт ничего не пишет в корпус: образец для проверки копируется из корпуса в
временный каталог байт в байт, а отчёты `check` остаются во временном каталоге.
Публикуется только агрегированный результат (без текстов исходников и абсолютных путей).

Пример (PowerShell, из корня language-review):

    uv run --locked python benchmarks/azurpilot/run_benchmark.py `
        --corpus E:\\project\\_lr-tools\\corpus `
        --languagetool http://127.0.0.1:8081 --llama-cpp http://127.0.0.1:8082 `
        --label after --output benchmarks/azurpilot/results-after.json

`--python` выбирает интерпретатор с установленным `language_review` (для замера старого
коммита — окружение его рабочей копии).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
CYRILLIC = re.compile("[А-Яа-яЁё]")


def normalized_sha256(path: Path) -> str:
    """SHA-256 содержимого с LF: результат не зависит от autocrlf рабочей копии."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_corpus(corpus: Path, manifest: dict[str, Any]) -> None:
    mismatches = []
    for group in ("docs", "sample"):
        for relative, expected in manifest[group]["files"].items():
            path = corpus / relative
            if not path.is_file() or normalized_sha256(path) != expected:
                mismatches.append(relative)
    if mismatches:
        raise SystemExit(
            "Корпус не совпадает с manifest.json (путь → хеш): "
            + ", ".join(mismatches[:5])
            + (" …" if len(mismatches) > 5 else "")
        )
    docs = {p.relative_to(corpus).as_posix() for p in (corpus / "docs").rglob("*.md")}
    if docs != set(manifest["docs"]["files"]):
        raise SystemExit("Набор файлов docs/ не совпадает с manifest.json.")


def run_check(
    python: str, config: Path, target: Path, report: Path, extra: list[str]
) -> tuple[int, float]:
    started = time.perf_counter()
    with report.open("wb") as stdout:
        process = subprocess.run(
            [python, "-m", "language_review", "--config", str(config), "check", str(target)]
            + ["--format", "json", *extra],
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUTF8": "1"},
            check=False,
        )
    return process.returncode, time.perf_counter() - started


def write_config(path: Path, engines: list[str], languagetool: str, llama_cpp: str) -> None:
    path.write_text(
        "[check]\n"
        f"engines = {json.dumps(engines)}\n"
        f'[check.languagetool]\nendpoint = "{languagetool}"\n'
        f'[check.llm.llama_cpp]\nendpoint = "{llama_cpp}"\n',
        encoding="utf-8",
    )


def engine_summary(report: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for engine in report["engines"]:
        details = engine["details"]
        keep = ("version", "model", "quantization", "n_ctx", "n_params", "server_build", "provider")
        keep += ("prompt_version", "prompt_template_sha256", "thinking", "seed", "batch_chars")
        keep += ("batch_items", "max_output_tokens", "level", "language", "program", "style")
        result.append(
            {
                "name": engine["name"],
                "status": engine["status"],
                "elapsed_s": round(engine["elapsed_ms"] / 1000, 1),
                "statistics": engine["statistics"],
                "tokens": engine["tokens"],
                "diagnostics": engine["diagnostics"],
                "details": {key: details[key] for key in keep if key in details},
            }
        )
    return result


def coverage(report: dict[str, Any]) -> dict[str, Any]:
    fragments = [item for doc in report["documents"] for item in doc["fragments"]]
    llm_checked_english = sum(
        item["checks"].get("llm", {}).get("status") == "checked"
        and item["checks"].get("languagetool", {}).get("reason") == "no_russian_text"
        for item in fragments
    )
    reasons: collections.Counter[str] = collections.Counter()
    for item in fragments:
        for engine, check in item["checks"].items():
            if check["status"] != "checked":
                reasons[f"{engine}:{check['status']}:{check['reason']}"] += 1
    summary = report["summary"]
    return {
        "status": report["status"],
        "documents": summary["documents"],
        "documents_by_status": {
            "clean": summary["clean"],
            "findings": summary["with_findings"],
            "partial": summary["partial"],
            "unavailable": summary["unavailable"],
            "error": summary["errors"],
        },
        "fragments": summary["fragments"],
        "units": summary["units"],
        "units_without_russian_text": summary["units_without_russian_text"],
        "fragments_english_checked_by_llm": llm_checked_english,
        "not_checked_fragment_engine_pairs": dict(sorted(reasons.items())),
        "findings": summary["findings"],
        "rejected": summary["rejected"],
    }


def in_scope(reviewed: dict[str, list[list[int]]], path: str, line: int) -> bool:
    return any(start <= line <= end for start, end in reviewed.get(path, []))


def matches(
    entry: dict[str, Any],
    path: str,
    source: dict[str, Any],
    text: str,
    *,
    same_category: bool = False,
) -> bool:
    start = source["range"]["start"]["line"]
    end = source["range"]["end"]["line"]
    if entry["path"] != path or not start <= entry["line"] <= end:
        return False
    if same_category and entry["category"] != source["category"]:
        return False
    expected, actual = entry["excerpt"].casefold(), text.casefold()
    return expected in actual or actual in expected


def score(report: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    reviewed = {item["path"]: item["lines"] for item in labels["reviewed"]}
    per_engine: dict[str, dict[str, Any]] = {}
    union = {"tp": set(), "fp": 0, "debatable": 0, "findings_in_scope": 0}
    false_kinds: collections.Counter[str] = collections.Counter()
    for document in report["documents"]:
        path = document["path"]
        for finding in document["findings"]:
            start = finding["range"]["start"]["line"]
            if not in_scope(reviewed, path, start):
                continue
            union["findings_in_scope"] += 1
            verdicts = set()
            for source in finding["sources"]:
                text = source["engine_excerpt"] or finding["excerpt"] or ""
                engine = per_engine.setdefault(
                    source["engine"],
                    {
                        "tp": set(),
                        "fp": 0,
                        "debatable": 0,
                        "sources_in_scope": 0,
                        "location_hits_other_essence": 0,
                        "fp_kinds": {},
                    },
                )
                engine["sources_in_scope"] += 1
                # Совпадение «по сути дефекта»: то же место и та же категория.
                defect = next(
                    (
                        d
                        for d in labels["defects"]
                        if matches(d, path, source, text, same_category=True)
                    ),
                    None,
                )
                if defect is None and any(
                    matches(d, path, source, text) for d in labels["defects"]
                ):
                    engine["location_hits_other_essence"] += 1
                debatable = next(
                    (d for d in labels["debatable"] if matches(d, path, source, text)), None
                )
                if defect is not None:
                    engine["tp"].add(defect["id"])
                    verdicts.add("tp:" + defect["id"])
                elif debatable is not None:
                    engine["debatable"] += 1
                    verdicts.add("debatable")
                else:
                    engine["fp"] += 1
                    script = "кириллица" if CYRILLIC.search(text) else "латиница"
                    kind = f"{source['category']}/{source['rule_id'] or '-'}/{script}"
                    engine["fp_kinds"][kind] = engine["fp_kinds"].get(kind, 0) + 1
                    false_kinds[f"{source['engine']}:{kind}"] += 1
            tps = {item for item in verdicts if item.startswith("tp:")}
            if tps:
                union["tp"].update(item[3:] for item in tps)
            elif "debatable" in verdicts:
                union["debatable"] += 1
            else:
                union["fp"] += 1
    defect_ids = {item["id"] for item in labels["defects"]}

    def finish(tp: set[str], fp: int, debatable: int, extra: dict[str, Any]) -> dict[str, Any]:
        total = len(tp) + fp
        return {
            "tp": len(tp),
            "fp": fp,
            "fn": len(defect_ids - tp),
            "debatable_hits": debatable,
            "precision": round(len(tp) / total, 4) if total else None,
            # Эталон из одного дефекта не позволяет оценить полноту: recall и F1 не вычисляются.
            "recall": None,
            "f1": None,
            **extra,
        }

    result = {
        name: finish(
            data["tp"],
            data["fp"],
            data["debatable"],
            {
                "sources_in_scope": data["sources_in_scope"],
                "location_hits_other_essence": data["location_hits_other_essence"],
                "fp_kinds": dict(sorted(data["fp_kinds"].items(), key=lambda x: -x[1])[:8]),
            },
        )
        for name, data in sorted(per_engine.items())
    }
    result["union"] = finish(
        union["tp"],
        union["fp"],
        union["debatable"],
        {"findings_in_scope": union["findings_in_scope"]},
    )
    return result


def write_manifest(corpus: Path, head: str) -> None:
    """Построить manifest.json: хеши docs/ и файлов образца из labels.json."""
    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
    docs = sorted(p.relative_to(corpus).as_posix() for p in (corpus / "docs").rglob("*.md"))
    sample = sorted({item["path"] for item in labels["reviewed"]})
    manifest = {
        "corpus": {
            "repository": "AzurPilotRu",
            "head": head,
            "export": "git archive <head>; хеши считаются по содержимому с LF",
        },
        "docs": {"files": {path: normalized_sha256(corpus / path) for path in docs}},
        "sample": {"files": {path: normalized_sha256(corpus / path) for path in sample}},
    }
    (HERE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--write-manifest", metavar="HEAD", help="создать manifest.json и выйти")
    parser.add_argument("--languagetool")
    parser.add_argument("--llama-cpp")
    parser.add_argument("--label")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        help="сохранить полные отчёты check (содержат цитаты исходников; не публиковать)",
    )
    parser.add_argument(
        "--rescore", type=Path, help="пересчитать score по сохранённому отчёту sample и выйти"
    )
    parser.add_argument("--skip-docs", action="store_true")
    parser.add_argument("--skip-sample", action="store_true")
    args = parser.parse_args()
    if (args.write_manifest or not args.rescore) and not args.corpus:
        parser.error("нужен --corpus")
    if args.write_manifest:
        write_manifest(args.corpus, args.write_manifest)
        return 0
    if args.rescore:
        labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
        report = json.loads(args.rescore.read_text(encoding="utf-8"))
        print(json.dumps(score(report, labels), ensure_ascii=False, indent=2))
        return 0
    if not (args.languagetool and args.llama_cpp and args.label and args.output):
        parser.error("нужны --languagetool, --llama-cpp, --label и --output")

    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
    verify_corpus(args.corpus, manifest)
    result: dict[str, Any] = {
        "label": args.label,
        "corpus_head": manifest["corpus"]["head"],
        "labels_sha256": hashlib.sha256((HERE / "labels.json").read_bytes()).hexdigest(),
    }
    with tempfile.TemporaryDirectory(prefix="language-review-bench-") as temp:
        work = Path(temp)
        if not args.skip_docs:
            config = work / "docs.toml"
            write_config(config, ["languagetool", "vale"], args.languagetool, args.llama_cpp)
            report_path = work / "docs.json"
            code, wall = run_check(args.python, config, args.corpus / "docs", report_path, [])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            result["docs_fast"] = {
                "engines_requested": ["languagetool", "vale"],
                "exit_code": code,
                "wall_s": round(wall, 1),
                "coverage": coverage(report),
                "engines": engine_summary(report),
            }
        if not args.skip_sample:
            sample = work / "sample"
            for relative in manifest["sample"]["files"]:
                target = sample / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(args.corpus / relative, target)
            config = work / "sample.toml"
            write_config(config, ["languagetool", "vale", "llm"], args.languagetool, args.llama_cpp)
            report_path = work / "sample.json"
            code, wall = run_check(args.python, config, sample, report_path, [])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if args.raw_dir:
                args.raw_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(report_path, args.raw_dir / f"sample-{args.label}.json")
            result["sample_all_engines"] = {
                "engines_requested": ["languagetool", "vale", "llm"],
                "exit_code": code,
                "wall_s": round(wall, 1),
                "coverage": coverage(report),
                "engines": engine_summary(report),
                "score": score(report, labels),
            }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
