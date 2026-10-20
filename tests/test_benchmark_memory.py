from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load_benchmark() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "benchmark-memory.py"
    spec = importlib.util.spec_from_file_location("benchmark_memory", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_reads_installed_revision_and_live_embedding_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = _load_benchmark()
    payloads: list[dict[str, Any] | None] = []

    def request_json(_url: str, *, payload: dict[str, Any] | None = None, **_kwargs: Any):
        payloads.append(payload)
        if payload is None:
            return SimpleNamespace(
                body={"models": [{"name": "qwen3-embedding:0.6b", "digest": "a" * 64}]}
            )
        return SimpleNamespace(body={"embeddings": [[1.0, 2.0, 3.0]]})

    class FakeEmbedder:
        def __init__(self, _store: Any) -> None:
            pass

        def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
            return [[1.0, 2.0, 3.0] for _ in texts]

    monkeypatch.setattr(benchmark, "request_json", request_json)
    monkeypatch.setattr(benchmark, "Embedder", FakeEmbedder)

    result = benchmark.benchmark("qwen3-embedding:0.6b")

    assert result["revision"] == "a" * 64
    assert result["dimensions"] == 3
    assert len(payloads) == 2
    assert payloads[1] == {
        "model": "qwen3-embedding:0.6b",
        "input": ["language-review embedding dimension probe"],
    }


def test_benchmark_reports_missing_model_instead_of_stop_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setattr(
        benchmark,
        "request_json",
        lambda *_args, **_kwargs: SimpleNamespace(body={"models": []}),
    )

    with pytest.raises(ValueError, match="model is not installed"):
        benchmark.benchmark("missing-model")
