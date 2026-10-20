"""Общий worker на собственной временной PostgreSQL БД; без гонки с другими тестами."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from conftest import JsonServer, RecordedRequest
from test_memory_integration import ConsolidationProvider

from language_review.config import CheckSettings
from language_review.memory.settings import MemorySettings
from language_review.memory.store import Store
from language_review.memory.worker import work

pytest_plugins = ["test_memory_integration"]

pytestmark = pytest.mark.skipif(
    not os.environ.get("LANGUAGE_REVIEW_MEMORY_TEST_DSN"),
    reason="NOT_RUN: изолированная PostgreSQL БД не задана",
)


@pytest.fixture
def worker_store(
    monkeypatch: pytest.MonkeyPatch, json_server: JsonServer, stored: dict[str, Any]
) -> Iterator[Store]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = "lr_worker_" + uuid4().hex
    with psycopg.connect(os.environ["LANGUAGE_REVIEW_MEMORY_TEST_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv(
                "LANGUAGE_REVIEW_WORKER_TEST_DSN",
                make_conninfo(os.environ["LANGUAGE_REVIEW_MEMORY_TEST_DSN"], dbname=name),
            )

            def respond(request: RecordedRequest) -> tuple[int, object]:
                if request.path == "/api/tags":
                    return 200, {"models": [{"name": "embeddinggemma:300m", "digest": "a" * 64}]}
                return 200, {"embeddings": [[1.0, 0.5] for _ in request.body["input"]]}

            json_server.responder = respond
            monkeypatch.setattr(
                "language_review.memory.worker.create_provider", lambda _: ConsolidationProvider()
            )
            store = Store(
                MemorySettings(
                    enabled=True,
                    dsn_env="LANGUAGE_REVIEW_WORKER_TEST_DSN",
                    embedding_endpoint=json_server.url,
                    embedding_model="embeddinggemma:300m",
                    embedding_revision="a" * 64,
                    embedding_dimensions=2,
                )
            )
            store.migrate()
            yield store
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def seed(store: Store, report: dict[str, Any], dataset: str = "synthetic") -> str:
    project, run = str(uuid4()), str(uuid4())
    store.begin(run, {"root-0001": project}, dataset, "live")
    store.ingest(run, report, {})
    return project


def test_all_projects_worker_discovers_future_projects_and_bounds_jobs_and_index(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    assert work(worker_store, None, CheckSettings(), limit=1)["jobs"] == []
    first = seed(worker_store, stored["report"])
    one = work(worker_store, None, CheckSettings(), limit=1)
    assert len(one["jobs"]) == 1 and one["jobs"][0]["status"] == "done"
    assert one["index"]["indexed"] == 1
    second = seed(worker_store, stored["report"])
    holdout = seed(worker_store, stored["report"], "holdout")
    rest = work(worker_store, None, CheckSettings(), limit=8)
    assert len(rest["jobs"]) == 3 and all(row["status"] == "done" for row in rest["jobs"])
    assert rest["status"] == "processed"
    for project in [first, second]:
        status = worker_store.status(project)
        assert status["jobs"] == [{"state": "done", "count": 2, "attempts": 2}]
        assert status["index"]["rules"] == status["index"]["indexed"] == 1
        assert status["findings"] == [{"state": "unverified", "count": 2}]
    assert worker_store.status(holdout)["jobs"] == []
    assert worker_store.status(holdout)["rules"] == []
    assert work(worker_store, None, CheckSettings(), limit=8)["jobs"] == []


def test_scoped_worker_keeps_other_projects_pending_and_global_worker_repairs_index(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    first = seed(worker_store, stored["report"])
    second = seed(worker_store, stored["report"])
    # An unavailable embedding service must leave consolidation and original events durable.
    worker_store.settings = replace(worker_store.settings, embedding_revision="b" * 64)
    outcome = work(worker_store, first, CheckSettings(), limit=8)
    assert outcome["status"] == "partial"
    assert outcome["index"]["errors"][0]["reason"] == "memory_embedding_revision_mismatch"
    assert worker_store.status(first)["jobs"] == [{"state": "done", "count": 2, "attempts": 2}]
    assert worker_store.status(second)["jobs"] == [{"state": "pending", "count": 2, "attempts": 0}]
    worker_store.settings = replace(worker_store.settings, embedding_revision="a" * 64)
    repaired = work(worker_store, None, CheckSettings(), limit=1)
    assert repaired["index"]["indexed"] == 1
    assert worker_store.status(first)["index"]["indexed"] == 1
