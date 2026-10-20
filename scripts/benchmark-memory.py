"""Синтетический retrieval benchmark настоящих локальных embedding/Hindsight.

Это диагностика пригодности retrieval, не независимая оценка языковой точности.
Результат сохраняйте вне checkout. Метки не отправляются ни одному backend.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from uuid import uuid4

from language_review.engine_http import EngineHttpError, request_json
from language_review.memory.search import Embedder
from language_review.memory.settings import MemorySettings
from language_review.memory.store import Store

PASSAGES = [
    "Подтверждено: праверить — опечатка; правильно проверить. Контекст: команда CLI.",
    "Отклонено: термин endpoint допустим в документации API проекта; перевод не требуется.",
    "Подтверждено: сервер принимает JSON, название формата сохраняется латиницей.",
    "Спорно: слово деплой в инструкции; нужна проверка принятой терминологии проекта.",
    "Подтверждено: два процесса используют общий мьютекс для синхронизации потоков.",
    "Исключение проекта: HTTP timeout обозначает таймаут запроса, а не время сборки.",
    "Подтверждено: команда возвращает ненулевой exit code при ошибке.",
    "Отклонено: UTF-16 и BOM — точные технические обозначения; их нельзя переводить.",
]
QUERIES = [
    ("В CLI написано праверить параметры", 0),
    ("Следует ли переводить endpoint в описании API?", 1),
    ("Формат обмена данными JSON", 2),
    ("Развёртывание приложения и термин деплой", 3),
    ("Блокировка общего ресурса между потоками", 4),
    ("Истёк срок ожидания HTTP-ответа", 5),
    ("Программа завершается с кодом ошибки", 6),
    ("Кодировка UTF-16 с маркером порядка байтов", 7),
]


def cosine(first: list[float], second: list[float]) -> float:
    return sum(a * b for a, b in zip(first, second, strict=True)) / math.sqrt(
        sum(a * a for a in first) * sum(b * b for b in second)
    )


def benchmark(model: str) -> dict:
    tags = request_json("http://127.0.0.1:11434/api/tags", timeout=10).body["models"]
    installed = next(
        (row for row in tags if isinstance(row, dict) and row.get("name") == model), None
    )
    if installed is None or not isinstance(installed.get("digest"), str):
        raise ValueError(f"Ollama model is not installed or has no digest: {model}")
    revision = installed["digest"]
    probe = request_json(
        "http://127.0.0.1:11434/api/embed",
        payload={"model": model, "input": ["language-review embedding dimension probe"]},
        timeout=120,
    ).body
    embeddings = probe.get("embeddings") if isinstance(probe, dict) else None
    if (
        not isinstance(embeddings, list)
        or len(embeddings) != 1
        or not isinstance(embeddings[0], list)
        or not embeddings[0]
    ):
        raise ValueError(f"Ollama returned no embedding dimensions for model: {model}")
    dimensions = len(embeddings[0])
    embedder = Embedder(
        Store(
            MemorySettings(
                embedding_model=model, embedding_revision=revision, embedding_dimensions=dimensions
            )
        )
    )
    start = time.perf_counter()
    passages = embedder.embed(PASSAGES)
    queries = embedder.embed([query for query, _ in QUERIES], query=True)
    ranks = []
    for vector, (_, expected) in zip(queries, QUERIES, strict=True):
        ranking = sorted(range(len(passages)), key=lambda index: -cosine(vector, passages[index]))
        ranks.append(ranking.index(expected) + 1)
    return {
        "model": model,
        "revision": revision,
        "dimensions": dimensions,
        "queries": len(ranks),
        "ranks": ranks,
        "recall_at_1": sum(rank == 1 for rank in ranks) / len(ranks),
        "recall_at_3": sum(rank <= 3 for rank in ranks) / len(ranks),
        "mrr": sum(1 / rank for rank in ranks) / len(ranks),
        "wall_ms": round((time.perf_counter() - start) * 1000),
    }


def hindsight(base: str) -> dict:
    bank = "language-review-benchmark-" + uuid4().hex
    start = time.perf_counter()
    operation = str(uuid4())
    payload = {
        "items": [
            {"content": text, "document_id": f"case-{index}"} for index, text in enumerate(PASSAGES)
        ],
        "async": True,
        "operation_id": operation,
    }
    result = {"bank": bank, "operation_id": operation}
    try:
        prefix = f"{base}/v1/default/banks/{bank}"
        receipt = request_json(prefix + "/memories", payload=payload, timeout=30).body
        replay = request_json(prefix + "/memories", payload=payload, timeout=30).body
        result.update(receipt=receipt, replay=replay)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            progress = request_json(prefix + f"/operations/{operation}", timeout=15).body
            result["operation"] = progress
            if progress.get("status") in {"completed", "failed"}:
                break
            time.sleep(2)
        documents = request_json(prefix + "/documents", timeout=15).body
        result["documents"] = documents
        results = []
        for query, expected in QUERIES:
            recalled = request_json(
                prefix + "/memories/recall",
                payload={"query": query, "max_tokens": 1500, "budget": "low"},
                timeout=30,
            ).body
            results.append({"expected_document": f"case-{expected}", "recall": recalled})
        result["retrieval"] = results
        docs = documents.get("items", [])
        zeros = sum(item.get("memory_unit_count", 0) == 0 for item in docs)
        hits = sum(
            bool(item["recall"].get("results"))
            and item["recall"]["results"][0].get("document_id") == item["expected_document"]
            for item in results
        )
        result["metrics"] = {
            "documents": len(docs),
            "zero_fact_documents": zeros,
            "facts": sum(item.get("memory_unit_count", 0) for item in docs),
            "recall_at_1": hits / len(QUERIES),
        }
        result["status"] = (
            "MEASURED"
            if progress.get("status") == "completed" and len(docs) == len(PASSAGES) and zeros == 0
            else "PARTIAL"
        )
    except EngineHttpError as exc:
        result.update(status="PARTIAL", reason=exc.code)
    result["wall_ms"] = round((time.perf_counter() - start) * 1000)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--hindsight")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = {
        "report_kind": "synthetic_retrieval_diagnostic",
        "embeddings": [benchmark(model) for model in arguments.model],
    }
    if arguments.hindsight:
        result["hindsight"] = hindsight(arguments.hindsight)
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "models": result["embeddings"],
                "hindsight_status": result.get("hindsight", {}).get("status"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
