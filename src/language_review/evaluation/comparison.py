"""Совместимые сравнения и ожидания только для детерминированного скоринга."""

from __future__ import annotations

from statistics import mean
from typing import Any

from language_review.errors import InputError
from language_review.evaluation.contracts import digest, validate


def compatible(first: dict[str, Any], second: dict[str, Any]) -> None:
    validate(first, "result")
    validate(second, "result")
    if first["compatibility"] != second["compatibility"]:
        raise InputError("Несовместимы корпус, разметка, split, тип отчёта или определения метрик.")


def _differences(first: Any, second: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(first, dict) and isinstance(second, dict):
        result = {}
        for key in sorted(first.keys() | second.keys()):
            result.update(_differences(first.get(key), second.get(key), prefix + key + "."))
        return result
    if first == second:
        return {}
    numeric = all(type(value) in (int, float) for value in (first, second))
    return {
        prefix.rstrip("."): {
            "baseline": first,
            "candidate": second,
            "delta": second - first if numeric else None,
        }
    }


def compare(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    compatible(first, second)

    def indices(result: dict[str, Any]) -> tuple[set[str], set[str], dict[str, list[str]]]:
        fp: set[str] = set()
        fn: set[str] = set()
        tp: dict[str, list[str]] = {}
        for case in result["cases"]:
            fn.update(case["id"] + "/" + label for label in case["fn"])
            for item in case["candidates"]:
                if item["outcome"] == "fp":
                    fp.add(case["id"] + "/" + item["key"])
                elif item["outcome"] == "tp":
                    tp.setdefault(case["id"] + "/" + item["label"], []).append(item["key"])
        return fp, fn, tp

    a_fp, a_fn, a_tp = indices(first)
    b_fp, b_fn, b_tp = indices(second)
    result = {
        "schema": "language-review.evaluation-comparison",
        "schema_version": "1.0",
        "compatibility": first["compatibility"],
        "new_fp": sorted(b_fp - a_fp),
        "removed_fp": sorted(a_fp - b_fp),
        "new_fn": sorted(b_fn - a_fn),
        "recovered_fn": sorted(a_fn - b_fn),
        "new_tp": sorted(b_tp.keys() - a_tp.keys()),
        "lost_tp": sorted(a_tp.keys() - b_tp.keys()),
        "changed_tp": sorted(key for key in a_tp.keys() & b_tp.keys() if a_tp[key] != b_tp[key]),
        "summary_deltas": _differences(first["summary"], second["summary"]),
        "strata_deltas": _differences(first["strata"], second["strata"]),
        "engine_deltas": _differences(
            {key: value["summary"] for key, value in first["engines"].items()},
            {key: value["summary"] for key, value in second["engines"].items()},
        ),
        "coverage_changes": _differences(
            {
                case["id"]: {
                    "state": case["state"],
                    "coverage": case["coverage"],
                    "reasons": case["reasons"],
                }
                for case in first["cases"]
            },
            {
                case["id"]: {
                    "state": case["state"],
                    "coverage": case["coverage"],
                    "reasons": case["reasons"],
                }
                for case in second["cases"]
            },
        ),
        "time": _differences(
            {
                "wall_clock_ms": first["cost"]["wall_clock_ms"],
                "engine_elapsed_ms": {
                    item["name"]: item["elapsed_ms"] for item in first["cost"]["engines"]
                },
            },
            {
                "wall_clock_ms": second["cost"]["wall_clock_ms"],
                "engine_elapsed_ms": {
                    item["name"]: item["elapsed_ms"] for item in second["cost"]["engines"]
                },
            },
        ),
    }
    validate(result, "comparison")
    return result


def expectation(result: dict[str, Any]) -> dict[str, Any]:
    validate(result, "result")
    # Изолируем детерминированный пересчёт сохранённого отчёта от live-таймингов.
    projection = {key: value for key, value in result.items() if key not in {"cost", "manifest"}}
    return {
        "schema": "language-review.eval-expectations",
        "schema_version": "1.0",
        "compatibility": result["compatibility"],
        "projection_sha256": digest(projection),
    }


def check_expectation(result: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    validate(expected, "expectations")
    actual = expectation(result)
    if actual["compatibility"] != expected["compatibility"]:
        raise InputError("Ожидания относятся к другому корпусу или определению метрик.")
    return {
        "status": "PASS" if actual == expected else "FAIL",
        "actual": actual,
        "expected": expected,
        "scope": "offline saved-report scoring only; no live stability claim",
    }


def distribution(results: list[dict[str, Any]]) -> dict[str, Any]:
    for result in results:
        compatible(results[0], result)
    values = {}
    for name in ("precision", "recall", "f1", "f0_5"):
        raw = [result["summary"]["pipeline_detection"][name] for result in results]
        observed = [value for value in raw if value is not None]
        values[name] = {
            "values": raw,
            "n": len(observed),
            "mean": mean(observed) if observed else None,
            "min": min(observed) if observed else None,
            "max": max(observed) if observed else None,
        }
    aggregate = {
        "schema": "language-review.evaluation-distribution",
        "schema_version": "1.0",
        "repetitions": len(results),
        "independent_stability_proven": False,
        "statuses": [result["status"] for result in results],
        "pipeline_detection": values,
        "unit": "one public check invocation on the same split; descriptive only",
    }
    validate(aggregate, "distribution")
    return aggregate
