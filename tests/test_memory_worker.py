"""Общий worker на собственной временной PostgreSQL БД; без гонки с другими тестами."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from conftest import JsonServer, RecordedRequest
from test_memory_integration import ConsolidationProvider

from language_review.check_models import ProbeResult
from language_review.config import CheckSettings
from language_review.engine_llm import ProviderReply
from language_review.memory.search import recall
from language_review.memory.settings import MemorySettings
from language_review.memory.store import Store
from language_review.memory.worker import process_one, work

pytest_plugins = ["test_memory_integration"]

pytestmark = pytest.mark.skipif(
    not os.environ.get("LANGUAGE_REVIEW_MEMORY_TEST_DSN"),
    reason="NOT_RUN: изолированная PostgreSQL БД не задана",
)
PRIVATE_DIAGNOSTIC_KEYS = {
    "excerpt",
    "context",
    "suggestions",
    "text",
    "prompt",
    "response",
    "dsn",
    "path",
}


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


class DiagnosticProvider:
    name = "diagnostic-fixture"
    endpoint = "http://127.0.0.1"
    model = "synthetic-model"

    def __init__(self, mode: str = "valid", *, prompt_tokens: int | None = None) -> None:
        self.mode = mode
        self.prompt_tokens = prompt_tokens
        self.completion_called = False

    def probe(self) -> ProbeResult:
        if self.mode == "probe_timeout":
            from language_review.engine_http import EngineHttpError

            raise EngineHttpError("timeout", "synthetic probe")
        if self.mode == "probe_redirect_refused":
            from language_review.engine_http import EngineHttpError

            raise EngineHttpError("redirect_refused", "synthetic probe")
        if self.mode == "probe_unavailable":
            return ProbeResult("unavailable")
        return ProbeResult("available")

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        del system
        self.completion_called = True
        from language_review.engine_http import EngineHttpError

        if self.mode == "timeout":
            raise EngineHttpError("timeout", "synthetic completion")
        if self.mode == "json_invalid":
            return ProviderReply("{", truncated=False)
        if self.mode == "duplicate_json_key":
            return ProviderReply('{"rules":[],"rules":[]}', truncated=False)
        if self.mode == "schema_invalid":
            return ProviderReply('{"accepted":true}', truncated=False)

        cases = json.loads(user)["cases"]
        category = cases[0]["category"]
        if self.mode == "empty":
            return ProviderReply('{"rules":[]}', truncated=False)
        text = "праверить — вероятная опечатка"
        evidence = ["c1"]
        if self.mode == "evidence_alias_unknown":
            schema["properties"]["rules"]["items"]["properties"]["evidence"]["items"][
                "enum"
            ].append("c999")
            evidence = ["c999"]
        elif self.mode == "evidence_outside_permitted":
            assert len(cases) > 1, "Для синтетического c2 требуется второе finding той же category."
            evidence = ["c2"]
        elif self.mode == "current_evidence_missing":
            assert len(cases) > 1, "Для синтетического c2 требуется второе finding той же category."
            evidence = ["c2"]
        elif self.mode == "category_mismatch":
            category = "synthetic-other-category"
            schema["properties"]["rules"]["items"]["properties"]["category"]["enum"] = [category]
        elif self.mode == "sanitize_changed":
            text = (
                "TOKEN=synthetic-secret; "
                r"C:\Users\synthetic\private.txt; "
                "postgresql://synthetic-user:synthetic-password@localhost/private"
            )
        elif self.mode == "empty_after_strip":
            text = "   "

        content = json.dumps(
            {
                "rules": [
                    {
                        "text": text,
                        "category": category,
                        "relation": "pattern",
                        "evidence": evidence,
                    }
                ]
            },
            ensure_ascii=False,
        )
        return ProviderReply(
            content,
            truncated=False,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=7 if self.prompt_tokens is not None else None,
        )


class RevisionChangingProvider(DiagnosticProvider):
    def __init__(self, store: Store, project: str) -> None:
        super().__init__()
        self.store = store
        self.project = project

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE lr_findings f SET revision=f.revision+1 FROM lr_jobs j "
                "WHERE j.finding_id=f.id AND j.state='running' AND f.project_id=%s "
                "RETURNING f.id",
                (self.project,),
            ).fetchone()
        assert changed is not None
        return super().complete(system, user, schema)


def latest_attempt(status: dict[str, Any], attempt_id: str | None = None) -> dict[str, Any]:
    diagnostics = status["attempt_diagnostics"]
    assert set(diagnostics) >= {"retained", "failure_distribution", "recent"}
    rows = diagnostics["recent"]
    if attempt_id is not None:
        rows = [row for row in rows if row["attempt_id"] == attempt_id]
    assert rows
    return rows[0]


@pytest.mark.parametrize(
    ("mode", "subreason"),
    [
        ("evidence_alias_unknown", "evidence_alias_unknown"),
        ("evidence_outside_permitted", "evidence_outside_permitted"),
        ("current_evidence_missing", "current_evidence_missing"),
        ("category_mismatch", "category_mismatch"),
        ("sanitize_changed", "sanitize_changed"),
        ("empty_after_strip", "empty_text_after_strip"),
    ],
)
def test_post_schema_failures_are_publicly_distinguishable_and_private(
    worker_store: Store,
    stored: dict[str, Any],
    mode: str,
    subreason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if mode == "evidence_outside_permitted":
        from language_review.memory import worker as worker_module

        original = worker_module._post_schema_subreason

        def inconsistent_alias(
            rule: dict[str, Any],
            aliases: dict[str, str],
            permitted: set[str],
            current_id: str,
            category: str,
        ) -> str | None:
            return original(
                rule,
                {**aliases, "c2": "synthetic-outside-permitted"},
                permitted,
                current_id,
                category,
            )

        monkeypatch.setattr(worker_module, "_post_schema_subreason", inconsistent_alias)
    project = seed(worker_store, stored["report"])
    provider = DiagnosticProvider(mode)
    before = recall(
        worker_store,
        project,
        "праверить",
        dataset="synthetic",
        hybrid=False,
        include_hypotheses=True,
    )
    before_status = worker_store.status(project)

    result = process_one(worker_store, project, CheckSettings(), provider)

    assert result and result["status"] == "retry_or_failed"
    assert provider.completion_called
    status = worker_store.status(project)
    attempt = latest_attempt(status, result["attempt_id"])
    assert attempt["job_id"] == result["job"]
    assert attempt["attempt"] == 1
    assert attempt["retry"] is False
    assert attempt["job_state"] == "pending"
    assert attempt["outcome"] == "failed"
    assert attempt["disposition"] == "retry"
    assert attempt["stage"] == "post_schema"
    assert attempt["error"] == "memory_llm_evidence_invalid"
    assert attempt["subreason"] == subreason
    assert attempt["stages"]["post_schema"]["outcome"] == "failed"
    assert any(
        row
        == {
            "error": "memory_llm_evidence_invalid",
            "subreason": subreason,
            "stage": "post_schema",
            "disposition": "retry",
            "count": 1,
        }
        for row in status["attempt_diagnostics"]["failure_distribution"]
    )
    assert status["durable_events"] == before_status["durable_events"]
    assert status["rules"] == []
    assert status["findings"] == before_status["findings"]
    assert (
        recall(
            worker_store,
            project,
            "праверить",
            dataset="synthetic",
            hybrid=False,
            include_hypotheses=True,
        )
        == before
    )
    assert not PRIVATE_DIAGNOSTIC_KEYS.intersection(attempt)
    if mode == "sanitize_changed":
        serialized = json.dumps(status, ensure_ascii=False, default=str)
        serialized_result = json.dumps(result, ensure_ascii=False, default=str)
        for private_value in (
            "synthetic-secret",
            "synthetic-password",
            "private.txt",
            "postgresql://",
            "TOKEN=",
        ):
            assert private_value not in serialized
            assert private_value not in serialized_result


@pytest.mark.parametrize(
    ("mode", "subreason"),
    [
        ("json_invalid", "json_invalid"),
        ("duplicate_json_key", "duplicate_json_key"),
        ("schema_invalid", "schema_validation_failed"),
    ],
)
def test_json_and_schema_failures_keep_stage_and_safe_subreason(
    worker_store: Store, stored: dict[str, Any], mode: str, subreason: str
) -> None:
    project = seed(worker_store, stored["report"])
    result = process_one(worker_store, project, CheckSettings(), DiagnosticProvider(mode))

    assert result and result["status"] == "retry_or_failed"
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["error"] == "memory_llm_schema_invalid"
    assert attempt["stage"] == "parse_schema"
    assert attempt["subreason"] == subreason
    assert attempt["outcome"] == "failed"
    assert attempt["stages"]["parse_schema"]["outcome"] == "failed"


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("probe_timeout", "memory_llm_timeout"),
        ("probe_redirect_refused", "memory_llm_redirect_refused"),
        ("probe_unavailable", "memory_llm_unavailable"),
    ],
)
def test_probe_failure_is_recorded_before_completion(
    worker_store: Store, stored: dict[str, Any], mode: str, error: str
) -> None:
    project = seed(worker_store, stored["report"])
    provider = DiagnosticProvider(mode)

    result = process_one(worker_store, project, CheckSettings(), provider)

    assert result and result["status"] == "retry_or_failed"
    assert not provider.completion_called
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["stage"] == "probe"
    assert attempt["error"] == error
    assert attempt["outcome"] == "failed"
    assert attempt["stages"]["probe"]["outcome"] == "failed"
    assert "completion" not in attempt["stages"]
    assert isinstance(attempt["stages"]["probe"]["duration_ms"], int)
    assert attempt["stages"]["probe"]["configured_limit_ms"] > 0


def test_attempt_history_keeps_timeout_after_successful_retry(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project = seed(worker_store, stored["report"])
    with worker_store.connect() as db:
        target = db.execute(
            "SELECT j.id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s ORDER BY j.id LIMIT 1",
            (project,),
        ).fetchone()["id"]
        db.execute("UPDATE lr_jobs SET state='done' WHERE id!=%s", (target,))

    failed = process_one(worker_store, project, CheckSettings(), DiagnosticProvider("timeout"))
    assert failed and failed["status"] == "retry_or_failed"
    with worker_store.connect() as db:
        db.execute("UPDATE lr_jobs SET available_at=clock_timestamp() WHERE id=%s", (target,))
    recovered = process_one(
        worker_store,
        project,
        CheckSettings(),
        DiagnosticProvider("valid", prompt_tokens=0),
    )

    assert recovered and recovered["status"] == "done"
    status = worker_store.status(project)
    attempts = [row for row in status["attempt_diagnostics"]["recent"] if row["job_id"] == target]
    assert {row["attempt"] for row in attempts} == {1, 2}
    first = next(row for row in attempts if row["attempt"] == 1)
    second = next(row for row in attempts if row["attempt"] == 2)
    assert first["retry"] is False
    assert first["disposition"] == "retry"
    assert first["stage"] == "completion"
    assert first["error"] == "memory_llm_timeout"
    assert first["stages"]["completion"]["outcome"] == "failed"
    assert second["retry"] is True
    assert second["disposition"] == "completed"
    assert second["job_state"] == "done"
    assert second["stages"]["completion"]["token_counts"] == {
        "prompt": 0,
        "completion": 7,
    }
    assert any(
        row["error"] == "memory_llm_timeout" and row["stage"] == "completion" and row["count"] == 1
        for row in status["attempt_diagnostics"]["failure_distribution"]
    )


def test_success_records_measured_stages_and_omits_unreported_tokens(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project = seed(worker_store, stored["report"])
    result = process_one(worker_store, project, CheckSettings(), DiagnosticProvider())

    assert result and result["status"] == "done"
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["outcome"] == "succeeded"
    assert attempt["disposition"] == "completed"
    for stage in (
        "claim",
        "context",
        "probe",
        "completion",
        "parse_schema",
        "post_schema",
        "persistence",
    ):
        recorded = attempt["stages"][stage]
        assert recorded["outcome"] == "succeeded"
        assert isinstance(recorded["duration_ms"], int)
        assert recorded["duration_ms"] >= 0
    assert attempt["stages"]["probe"]["configured_limit_ms"] > 0
    assert attempt["stages"]["completion"]["configured_limit_ms"] > 0
    assert "token_counts" not in attempt["stages"]["completion"]
    assert status_has_no_rule_text(worker_store.status(project))


def test_persistence_failure_keeps_failed_stage_and_rolls_back_hypothesis(
    worker_store: Store, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review.memory.store import MemoryFeatureError

    project = seed(worker_store, stored["report"])
    original = worker_store.record_attempt

    def fail_persistence_commit(
        job: dict[str, Any], diagnostic: dict[str, Any], db: Any | None = None
    ) -> None:
        if diagnostic["stage"] == "persistence" and diagnostic["outcome"] == "succeeded":
            raise MemoryFeatureError("memory_database_failed")
        original(job, diagnostic, db)

    monkeypatch.setattr(worker_store, "record_attempt", fail_persistence_commit)
    result = process_one(worker_store, project, CheckSettings(), DiagnosticProvider())

    assert result and result["status"] == "retry_or_failed"
    assert result["reason"] == "memory_database_failed"
    status = worker_store.status(project)
    attempt = latest_attempt(status, result["attempt_id"])
    assert attempt["outcome"] == "failed"
    assert attempt["disposition"] == "retry"
    assert attempt["stage"] == "persistence"
    assert attempt["error"] == "memory_database_failed"
    assert attempt["stages"]["persistence"]["outcome"] == "failed"
    assert isinstance(attempt["stages"]["persistence"]["duration_ms"], int)
    assert status["rules"] == []


def status_has_no_rule_text(status: dict[str, Any]) -> bool:
    diagnostics = status["attempt_diagnostics"]
    return all(
        not PRIVATE_DIAGNOSTIC_KEYS.intersection(attempt) for attempt in diagnostics["recent"]
    )


def test_rules_empty_remains_successful_noop_and_scoped_diagnostics(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project = seed(worker_store, stored["report"])
    other_project = seed(worker_store, stored["report"])
    before = recall(
        worker_store,
        project,
        "праверить",
        dataset="synthetic",
        hybrid=False,
        include_hypotheses=True,
    )

    result = process_one(worker_store, project, CheckSettings(), DiagnosticProvider("empty"))

    assert result and result["status"] == "done" and result["rules"] == 0
    status = worker_store.status(project)
    attempt = latest_attempt(status, result["attempt_id"])
    assert attempt["outcome"] == "succeeded"
    assert attempt["disposition"] == "completed"
    assert status["rules"] == []
    assert not status["recent_errors"]
    assert (
        recall(
            worker_store,
            project,
            "праверить",
            dataset="synthetic",
            hybrid=False,
            include_hypotheses=True,
        )
        == before
    )
    other = worker_store.status(other_project)["attempt_diagnostics"]
    assert other["retained"] == 0
    assert other["recent"] == []
    assert other["failure_distribution"] == []


def test_work_records_project_batch_indexing_without_provider_usage(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project = seed(worker_store, stored["report"])

    result = work(worker_store, project, CheckSettings(), limit=2)

    assert result["status"] == "processed"
    assert result["index"]["indexed"] >= 1
    status = worker_store.status(project)
    attempts = status["attempt_diagnostics"]["recent"]
    assert attempts
    for attempt in attempts:
        assert "token_counts" not in attempt["stages"]["completion"]
        if "indexing" in attempt["stages"]:
            indexing = attempt["stages"]["indexing"]
            assert indexing["scope"] == "project_batch"
            assert indexing["outcome"] == "succeeded"
            assert isinstance(indexing["duration_ms"], int)
    assert any("indexing" in attempt["stages"] for attempt in attempts)


def test_revision_stale_done_attempt_does_not_receive_indexing_stage(
    worker_store: Store,
    stored: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = seed(worker_store, stored["report"])
    provider = RevisionChangingProvider(worker_store, project)
    monkeypatch.setattr("language_review.memory.worker.create_provider", lambda _: provider)

    result = work(worker_store, project, CheckSettings(), limit=1)

    assert len(result["jobs"]) == 1
    job = result["jobs"][0]
    assert job["status"] == "done"
    assert job["stale"] is True
    attempt = latest_attempt(worker_store.status(project), job["attempt_id"])
    assert attempt["job_state"] == "done"
    assert attempt["outcome"] == "stale"
    assert attempt["disposition"] == "stale"
    assert "indexing" not in attempt["stages"]


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


def test_health_ignores_physical_row_order_and_still_rejects_drift(worker_store: Store) -> None:
    from language_review.memory.store import MemoryFeatureError

    with worker_store.connect() as db:
        # Перезапись первой версии переносит её в конец heap: seq scan без ORDER BY даёт 2, 1.
        first = db.execute(
            "DELETE FROM lr_migrations WHERE version=1 RETURNING version,checksum,applied_at"
        ).fetchone()
        db.execute(
            "INSERT INTO lr_migrations(version,checksum,applied_at) "
            "VALUES (%(version)s,%(checksum)s,%(applied_at)s)",
            first,
        )
        physical = db.execute("SELECT version FROM lr_migrations").fetchall()
    assert [row["version"] for row in physical] == [2, 3, 1]
    assert worker_store.health()["schema_version"] == 3
    with worker_store.connect() as db:
        db.execute("UPDATE lr_migrations SET checksum='drift' WHERE version=3")
    with pytest.raises(MemoryFeatureError, match="memory_migration_required"):
        worker_store.health()
    with worker_store.connect() as db:
        db.execute("DELETE FROM lr_migrations WHERE version=3")
    with pytest.raises(MemoryFeatureError, match="memory_migration_required"):
        worker_store.health()


def test_scoped_worker_keeps_other_projects_pending_and_global_worker_repairs_index(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    first = seed(worker_store, stored["report"])
    second = seed(worker_store, stored["report"])
    # Недоступный embedding-сервис не должен терять консолидацию и исходные события.
    worker_store.settings = replace(worker_store.settings, embedding_revision="b" * 64)
    outcome = work(worker_store, first, CheckSettings(), limit=8)
    assert outcome["status"] == "partial"
    assert outcome["index"]["errors"][0]["reason"] == "memory_embedding_revision_mismatch"
    assert worker_store.status(first)["jobs"] == [{"state": "done", "count": 2, "attempts": 2}]
    assert worker_store.status(second)["jobs"] == [{"state": "pending", "count": 2, "attempts": 0}]
    diagnostics = worker_store.status(first)["attempt_diagnostics"]
    indexing_failures = [
        row
        for row in diagnostics["recent"]
        if row["stages"].get("indexing", {}).get("outcome") == "failed"
    ]
    assert len(indexing_failures) == 2
    assert all(row["outcome"] == "succeeded" for row in indexing_failures)
    assert all(
        row["stages"]["indexing"]["error"] == "memory_embedding_revision_mismatch"
        and isinstance(row["stages"]["indexing"]["duration_ms"], int)
        for row in indexing_failures
    )
    assert {
        "error": "memory_embedding_revision_mismatch",
        "subreason": None,
        "stage": "indexing",
        "disposition": "completed",
        "count": 2,
    } in diagnostics["failure_distribution"]
    worker_store.settings = replace(worker_store.settings, embedding_revision="a" * 64)
    repaired = work(worker_store, None, CheckSettings(), limit=1)
    assert repaired["index"]["indexed"] == 1
    assert worker_store.status(first)["index"]["indexed"] == 1
