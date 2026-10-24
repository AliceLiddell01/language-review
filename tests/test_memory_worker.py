"""Общий worker на собственной временной PostgreSQL БД; без гонки с другими тестами."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from conftest import JsonServer, RecordedRequest
from test_memory_integration import ConsolidationProvider

from language_review.check_models import ProbeResult
from language_review.config import CheckSettings
from language_review.engine_llm import ProviderReply
from language_review.memory.cli import run as run_memory
from language_review.memory.cli import submit_decision
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


def contains_private_diagnostic_keys(value: Any, *, stage_map: bool = False) -> bool:
    if isinstance(value, dict):
        return any(
            (key in PRIVATE_DIAGNOSTIC_KEYS and not (stage_map and key == "context"))
            or contains_private_diagnostic_keys(item, stage_map=key == "stages")
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_private_diagnostic_keys(item, stage_map=stage_map) for item in value)
    return False


def test_private_diagnostic_key_check_recurses_into_stage_data() -> None:
    assert contains_private_diagnostic_keys({"stages": {"completion": {"text": "private"}}})
    assert contains_private_diagnostic_keys({"stages": {"text": {"duration_ms": 1}}})
    assert contains_private_diagnostic_keys({"metadata": {"context": "private"}})
    assert not contains_private_diagnostic_keys({"stages": {"context": {"duration_ms": 1}}})


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
        if self.mode == "deep_json":
            nested = "[" * 10000 + '"TOKEN=synthetic-depth-secret"' + "]" * 10000
            return ProviderReply(
                '{"rules":[{"text":"Синтетическое правило","category":"spelling",'
                '"relation":"pattern","evidence":[' + nested + "," + nested + "]}]}",
                truncated=False,
            )

        cases = json.loads(user)["cases"]
        category = cases[0]["category"]
        if self.mode == "empty":
            return ProviderReply('{"rules":[]}', truncated=False)
        text = "праверить — вероятная опечатка"
        evidence = ["c1"]
        if self.mode == "sanitize_changed":
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


class RuleSelectionProvider:
    """Возвращает фиксированный структурный ответ для тестов выбора правила."""

    name = "selection-fixture"
    endpoint = "http://127.0.0.1"
    model = "synthetic-model"

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.cases: list[dict[str, Any]] = []

    def probe(self) -> ProbeResult:
        return ProbeResult("available")

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        del system, schema
        self.cases = json.loads(user)["cases"]
        rules = deepcopy(self.rules)
        for rule in rules:
            if rule["category"] is None:
                rule["category"] = self.cases[0]["category"]
        return ProviderReply(json.dumps({"rules": rules}, ensure_ascii=False), truncated=False)


class FailOnceRuleSelectionProvider(RuleSelectionProvider):
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        super().__init__(rules)
        self.calls = 0

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        self.calls += 1
        if self.calls == 1:
            from language_review.engine_http import EngineHttpError

            raise EngineHttpError("timeout", "synthetic completion")
        return super().complete(system, user, schema)


class RevisionChangingRuleSelectionProvider(RuleSelectionProvider):
    def __init__(self, store: Store, project: str, rules: list[dict[str, Any]]) -> None:
        super().__init__(rules)
        self.store = store
        self.project = project

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        reply = super().complete(system, user, schema)
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE lr_findings f SET revision=f.revision+1 FROM lr_jobs j "
                "WHERE j.finding_id=f.id AND j.state='running' AND f.project_id=%s "
                "RETURNING f.id",
                (self.project,),
            ).fetchone()
        assert changed is not None
        return reply


class LeaseReclaimingRuleSelectionProvider(RuleSelectionProvider):
    def __init__(self, store: Store, project: str, rules: list[dict[str, Any]]) -> None:
        super().__init__(rules)
        self.store = store
        self.project = project
        self.reclaimed: dict[str, Any] | None = None

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        reply = super().complete(system, user, schema)
        with self.store.connect() as db:
            expired = db.execute(
                "UPDATE lr_jobs j SET lease_until=clock_timestamp()-interval '1 second' "
                "FROM lr_findings f WHERE j.finding_id=f.id AND f.project_id=%s "
                "AND j.state='running' RETURNING j.id",
                (self.project,),
            ).fetchone()
        assert expired is not None
        self.reclaimed = self.store.claim(self.project, lease_seconds=300)
        assert self.reclaimed is not None
        return reply


def seventeen_case_report(report: dict[str, Any], *, excerpt: str | None = None) -> dict[str, Any]:
    """Расширяет отчёт синтетическими документами до c1…c17."""
    result = deepcopy(report)
    template_item = deepcopy(result["items"][0])
    template_document = deepcopy(
        next(
            document
            for document in result["analysis"]["documents"]
            if document["id"] == template_item["document_id"]
        )
    )
    root = template_document["root"]
    if excerpt is not None:
        for item in result["items"]:
            item["excerpt"] = excerpt
            for evidence in item["evidence"]:
                evidence["source_excerpt"] = excerpt
    for index in range(3, 18):
        document_id = f"synthetic-document-{index}"
        document = deepcopy(template_document)
        document.update(id=document_id, path=f"synthetic/case-{index}.rs")
        result["analysis"]["documents"].append(document)
        item = deepcopy(template_item)
        item.update(id=f"synthetic-finding-{index}", document_id=document_id)
        result["items"].append(item)
    assert root == result["analysis"]["roots"][0]["id"]
    assert len(result["items"]) == 17
    return result


def seed_seventeen_cases(store: Store, report: dict[str, Any]) -> tuple[str, str, str]:
    project, run = str(uuid4()), str(uuid4())
    report = seventeen_case_report(report)
    store.begin(run, {"root-0001": project}, "synthetic", "live")
    store.ingest(run, report, {})
    with store.connect() as db:
        current = db.execute(
            "SELECT j.id,j.finding_id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s ORDER BY j.created_at,j.id LIMIT 1",
            (project,),
        ).fetchone()
        db.execute(
            "UPDATE lr_jobs SET state='done' WHERE id IN ("
            "SELECT j.id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s AND j.id!=%s)",
            (project, current["id"]),
        )
    return project, str(current["finding_id"]), run


def seed_seventeen_decision_cases(
    store: Store,
    report: dict[str, Any],
    *,
    current_status: str,
    related_status: str,
    excerpt: str | None = None,
) -> tuple[str, str, str]:
    project, run = str(uuid4()), str(uuid4())
    report = seventeen_case_report(report, excerpt=excerpt)
    store.begin(run, {"root-0001": project}, "synthetic", "live")
    store.ingest(run, report, {})
    with store.connect() as db:
        current = db.execute(
            "SELECT j.finding_id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s ORDER BY j.created_at,j.id LIMIT 1",
            (project,),
        ).fetchone()
        occurrences = db.execute(
            "SELECT item_id,finding_id FROM lr_occurrences WHERE run_id=%s", (run,)
        ).fetchall()
    current_finding = str(current["finding_id"])
    for occurrence in occurrences:
        state = current_status if occurrence["finding_id"] == current_finding else related_status
        submit_decision(
            store,
            {
                "schema": "language-review.decision",
                "schema_version": "1.0",
                "project_id": project,
                "run_id": run,
                "item_id": occurrence["item_id"],
                "decision": {
                    "id": str(uuid4()),
                    "expected_revision": 0,
                    "status": state,
                    "actor": "synthetic-reviewer",
                    "reason": "Синтетическая проверка факта для теста консолидации.",
                    "suggestion": None,
                    "scope": "project",
                    "public_rule": None,
                },
            },
        )
    with store.connect() as db:
        current_job = db.execute(
            "SELECT j.id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE j.finding_id=%s AND j.revision=f.revision AND f.project_id=%s",
            (current_finding, project),
        ).fetchone()
        db.execute(
            "UPDATE lr_jobs SET state='done' WHERE id!=%s AND finding_id IN "
            "(SELECT id FROM lr_findings WHERE project_id=%s)",
            (current_job["id"], project),
        )
    return project, current_finding, run


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
        ("sanitize_changed", "sanitize_changed"),
        ("empty_after_strip", "empty_text_after_strip"),
    ],
)
def test_post_schema_failures_are_publicly_distinguishable_and_private(
    worker_store: Store,
    stored: dict[str, Any],
    mode: str,
    subreason: str,
) -> None:
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
    assert not contains_private_diagnostic_keys(attempt)
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


def test_current_case_rule_survives_unrelated_rule_for_seventeen_cases(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project, current_finding, _ = seed_seventeen_cases(worker_store, stored["report"])
    provider = FailOnceRuleSelectionProvider(
        [
            {
                "text": "Имя защищённой команды сохраняется без исправления.",
                "category": None,
                "relation": "terminology",
                "evidence": ["c1"],
            },
            {
                "text": "Большинство похожих случаев подтверждает общее правило.",
                "category": None,
                "relation": "pattern",
                "evidence": [f"c{index}" for index in range(2, 18)],
            },
        ]
    )
    before = _primary_worker_state(worker_store)

    failed = process_one(worker_store, project, CheckSettings(), provider)

    assert failed and failed["status"] == "retry_or_failed"
    assert failed["reason"] == "memory_llm_timeout"
    first_attempt = latest_attempt(worker_store.status(project), failed["attempt_id"])
    assert first_attempt["stage"] == "completion"
    assert first_attempt["disposition"] == "retry"
    with worker_store.connect() as db:
        db.execute(
            "UPDATE lr_jobs SET available_at=clock_timestamp() WHERE id=%s",
            (failed["job"],),
        )

    result = process_one(worker_store, project, CheckSettings(), provider)

    selection = {"received": 2, "selected": 1, "excluded_current_evidence_missing": 1}
    assert result and result["status"] == "done" and result["rules"] == 1
    assert result["job"] == failed["job"]
    assert result["rule_selection"] == selection
    assert [case["id"] for case in provider.cases] == [f"c{index}" for index in range(1, 18)]
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["outcome"] == "succeeded"
    assert attempt["disposition"] == "completed"
    assert attempt["subreason"] is None
    assert attempt["stages"]["post_schema"]["outcome"] == "succeeded"
    assert attempt["stages"]["post_schema"]["rule_selection"] == selection
    assert attempt["job_state"] == "done"
    attempts = [
        row
        for row in worker_store.status(project)["attempt_diagnostics"]["recent"]
        if row["job_id"] == result["job"]
    ]
    assert [row["attempt"] for row in attempts] == [2, 1]
    assert attempts[1]["outcome"] == "failed" and attempts[1]["error"] == "memory_llm_timeout"

    with worker_store.connect() as db:
        hypotheses = db.execute(
            "SELECT * FROM lr_rules WHERE project_id=%s AND kind='hypothesis' "
            "AND state='hypothesis' ORDER BY id",
            (project,),
        ).fetchall()
        history = db.execute(
            "SELECT h.* FROM lr_rule_history h JOIN lr_rules r ON r.id=h.rule_id "
            "WHERE r.project_id=%s AND r.kind='hypothesis' ORDER BY h.rule_id,h.revision",
            (project,),
        ).fetchall()
    assert len(hypotheses) == 1
    assert hypotheses[0]["text"] == "Имя защищённой команды сохраняется без исправления."
    assert hypotheses[0]["payload"]["relation"] == "terminology"
    assert hypotheses[0]["payload"]["evidence"] == [current_finding]
    assert hypotheses[0]["finding_id"] == current_finding
    assert len(history) == 1 and history[0]["revision"] == 1
    assert process_one(worker_store, project, CheckSettings(), provider) is None
    assert _primary_worker_state(worker_store) == before


@pytest.mark.parametrize(
    (
        "scenario",
        "current_status",
        "related_status",
        "excerpt",
        "current_relation",
        "related_relation",
    ),
    [
        ("protected-command", "rejected", "accepted", "Invoke-Review", "terminology", "pattern"),
        ("ordinary-typo", "accepted", "rejected", "праверить", "pattern", "exception"),
        ("disputed", "disputed", "accepted", "праверить", None, None),
    ],
)
def test_decision_facts_preserve_polarity_and_allow_abstention(
    worker_store: Store,
    stored: dict[str, Any],
    scenario: str,
    current_status: str,
    related_status: str,
    excerpt: str,
    current_relation: str | None,
    related_relation: str | None,
) -> None:
    project, current_finding, _ = seed_seventeen_decision_cases(
        worker_store,
        stored["report"],
        current_status=current_status,
        related_status=related_status,
        excerpt=excerpt,
    )
    rules: list[dict[str, Any]] = []
    if current_relation == "terminology":
        rules = [
            {
                "text": "Имя команды Invoke-Review сохраняется без исправления.",
                "category": None,
                "relation": current_relation,
                "evidence": ["c1"],
            },
            {
                "text": "Большинство приняло исправленное имя как правило для всех команд.",
                "category": None,
                "relation": related_relation,
                "evidence": [f"c{index}" for index in range(2, 18)],
            },
        ]
    elif current_relation == "pattern":
        rules = [
            {
                "text": "Слово праверить — опечатка в обычной прозе.",
                "category": None,
                "relation": current_relation,
                "evidence": ["c1"],
            },
            {
                "text": "Отклонённые предложения относятся к отдельным исключениям.",
                "category": None,
                "relation": related_relation,
                "evidence": [f"c{index}" for index in range(2, 18)],
            },
        ]
    provider = RuleSelectionProvider(rules)
    before = _primary_worker_state(worker_store)

    result = process_one(worker_store, project, CheckSettings(), provider)

    assert result and result["status"] == "done"
    assert [case["decision"]["status"] for case in provider.cases] == [
        current_status,
        *([related_status] * 16),
    ]
    expected_selection = {
        "received": len(rules),
        "selected": int(current_relation is not None),
        "excluded_current_evidence_missing": int(current_relation is not None),
    }
    assert result["rule_selection"] == expected_selection
    assert result["rules"] == expected_selection["selected"]
    with worker_store.connect() as db:
        hypotheses = db.execute(
            "SELECT * FROM lr_rules WHERE project_id=%s AND kind='hypothesis' "
            "AND state='hypothesis' ORDER BY id",
            (project,),
        ).fetchall()
        current = db.execute(
            "SELECT state FROM lr_findings WHERE id=%s AND project_id=%s",
            (current_finding, project),
        ).fetchone()
    assert current["state"] == current_status
    if current_relation is None:
        assert hypotheses == []
        assert result["rule_selection"] == {
            "received": 0,
            "selected": 0,
            "excluded_current_evidence_missing": 0,
        }
    else:
        assert len(hypotheses) == 1
        assert hypotheses[0]["state"] == "hypothesis"
        assert hypotheses[0]["payload"]["relation"] == current_relation
        assert hypotheses[0]["payload"]["evidence"] == [current_finding]
    ordinary = recall(worker_store, project, excerpt, dataset="synthetic", hybrid=False)
    assert not [row for row in ordinary["results"] if row["kind"] == "hypothesis"]
    assert _primary_worker_state(worker_store) == before


def test_majority_only_rule_completes_without_retry_or_publication(
    worker_store: Store, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _ = seed_seventeen_cases(worker_store, stored["report"])
    provider = RuleSelectionProvider(
        [
            {
                "text": "Большинство похожих случаев подтверждает общее правило.",
                "category": None,
                "relation": "pattern",
                "evidence": [f"c{index}" for index in range(2, 18)],
            }
        ]
    )
    monkeypatch.setattr("language_review.memory.worker.create_provider", lambda _: provider)

    result = work(worker_store, project, CheckSettings(), limit=1)

    assert result["status"] == "processed"
    assert len(result["jobs"]) == 1
    job = result["jobs"][0]
    assert job["status"] == "done" and job["rules"] == 0
    assert job["rule_selection"] == {
        "received": 1,
        "selected": 0,
        "excluded_current_evidence_missing": 1,
    }
    status = worker_store.status(project)
    attempt = latest_attempt(status, job["attempt_id"])
    assert attempt["job_state"] == "done"
    assert attempt["attempt"] == 1
    assert attempt["outcome"] == "succeeded"
    assert attempt["disposition"] == "completed"
    assert attempt["subreason"] is None
    assert status["rules"] == []
    assert status["durable_events"] == len(_primary_worker_state(worker_store)["events"])


def test_c1_membership_does_not_prove_semantic_support(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project, current_finding, _ = seed_seventeen_cases(worker_store, stored["report"])
    provider = RuleSelectionProvider(
        [
            {
                "text": "Все проекты всегда обязаны использовать одно глобальное написание.",
                "category": None,
                "relation": "pattern",
                "evidence": ["c1"],
            }
        ]
    )

    result = process_one(worker_store, project, CheckSettings(), provider)

    assert result and result["status"] == "done" and result["rules"] == 1
    assert result["rule_selection"] == {
        "received": 1,
        "selected": 1,
        "excluded_current_evidence_missing": 0,
    }
    with worker_store.connect() as db:
        hypotheses = db.execute(
            "SELECT * FROM lr_rules WHERE project_id=%s AND kind='hypothesis' "
            "AND state='hypothesis' ORDER BY id",
            (project,),
        ).fetchall()
    assert len(hypotheses) == 1
    [hypothesis] = hypotheses
    assert (
        hypothesis["text"] == "Все проекты всегда обязаны использовать одно глобальное написание."
    )
    assert hypothesis["payload"]["evidence"] == [current_finding]
    assert hypothesis["kind"] == "hypothesis" and hypothesis["state"] == "hypothesis"
    ordinary = recall(
        worker_store,
        project,
        "одно глобальное написание",
        dataset="synthetic",
        hybrid=False,
    )
    diagnostic = recall(
        worker_store,
        project,
        "одно глобальное написание",
        dataset="synthetic",
        hybrid=False,
        include_hypotheses=True,
    )
    assert all(row["kind"] != "hypothesis" for row in ordinary["results"])
    assert hypothesis["id"] in {row["id"] for row in diagnostic["results"]}


@pytest.mark.parametrize("interruption", ["revision", "lease"])
def test_selected_split_cannot_publish_after_revision_or_lease_changes(
    worker_store: Store,
    stored: dict[str, Any],
    interruption: str,
) -> None:
    project, _, _ = seed_seventeen_cases(worker_store, stored["report"])
    rules = [
        {
            "text": "Текущее правило с подтверждением c1.",
            "category": None,
            "relation": "pattern",
            "evidence": ["c1"],
        },
        {
            "text": "Отдельное правило большинства без текущего подтверждения.",
            "category": None,
            "relation": "exception",
            "evidence": [f"c{index}" for index in range(2, 18)],
        },
    ]
    provider: RuleSelectionProvider
    if interruption == "revision":
        provider = RevisionChangingRuleSelectionProvider(worker_store, project, rules)
    else:
        provider = LeaseReclaimingRuleSelectionProvider(worker_store, project, rules)

    result = process_one(worker_store, project, CheckSettings(), provider)

    selection = {"received": 2, "selected": 1, "excluded_current_evidence_missing": 1}
    assert result is not None
    assert result["diagnostic"]["stages"]["post_schema"]["rule_selection"] == selection
    assert worker_store.status(project)["rules"] == []
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["stage"] == "persistence"
    if interruption == "revision":
        assert result["status"] == "done" and result["stale"] is True
        assert attempt["outcome"] == "stale"
        assert attempt["disposition"] == "stale"
        assert attempt["stages"]["persistence"]["outcome"] == "stale"
    else:
        assert result["status"] == "retry_or_failed"
        assert result["reason"] == "memory_worker_lease_lost"
        assert attempt["outcome"] == "stale"
        assert attempt["disposition"] == "stale"
        assert isinstance(provider, LeaseReclaimingRuleSelectionProvider)
        assert provider.reclaimed and provider.reclaimed["attempt"] == 2
    with worker_store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) AS count FROM lr_rules WHERE project_id=%s "
                "AND kind='hypothesis' AND state='hypothesis'",
                (project,),
            ).fetchone()["count"]
            == 0
        )


@pytest.mark.parametrize(
    ("unsafe_sibling", "expected_stage", "expected_subreason"),
    [
        ("sanitize_changed", "post_schema", "sanitize_changed"),
        ("empty_after_strip", "post_schema", "empty_text_after_strip"),
        ("unknown_alias", "parse_schema", "schema_validation_failed"),
        ("invalid_relation", "parse_schema", "schema_validation_failed"),
    ],
)
def test_unsafe_sibling_rejects_whole_response_before_current_rule_persists(
    worker_store: Store,
    stored: dict[str, Any],
    unsafe_sibling: str,
    expected_stage: str,
    expected_subreason: str,
) -> None:
    project = seed(worker_store, stored["report"])
    safe_current = {
        "text": "Синтетическое правило для текущего случая.",
        "category": None,
        "relation": "pattern",
        "evidence": ["c1"],
    }
    unsafe = {
        "text": "Синтетическое правило для соседнего случая.",
        "category": None,
        "relation": "pattern",
        "evidence": ["c2"],
    }
    if unsafe_sibling == "sanitize_changed":
        unsafe["text"] = "TOKEN=synthetic-secret; правило"
    elif unsafe_sibling == "empty_after_strip":
        unsafe["text"] = "   "
    elif unsafe_sibling == "unknown_alias":
        unsafe["evidence"] = ["not-a-case"]
    else:
        unsafe["relation"] = "unsupported-relation"
    before = _primary_worker_state(worker_store)

    result = process_one(
        worker_store,
        project,
        CheckSettings(),
        RuleSelectionProvider([safe_current, unsafe]),
    )

    assert result and result["status"] == "retry_or_failed"
    status = worker_store.status(project)
    attempt = latest_attempt(status, result["attempt_id"])
    assert attempt["outcome"] == "failed"
    assert attempt["disposition"] == "retry"
    assert attempt["stage"] == expected_stage
    assert attempt["subreason"] == expected_subreason
    assert status["rules"] == []
    assert _primary_worker_state(worker_store) == before
    serialized = json.dumps([result, status], ensure_ascii=False, default=str)
    assert "synthetic-secret" not in serialized
    assert not contains_private_diagnostic_keys(attempt)


def _primary_worker_state(store: Store) -> dict[str, list[dict[str, Any]]]:
    with store.connect() as db:
        return {
            "events": db.execute("SELECT * FROM lr_events ORDER BY id").fetchall(),
            "findings": db.execute("SELECT * FROM lr_findings ORDER BY id").fetchall(),
            "decisions": db.execute("SELECT * FROM lr_decisions ORDER BY id").fetchall(),
            "case_rules": db.execute(
                "SELECT * FROM lr_rules WHERE kind='case' ORDER BY id"
            ).fetchall(),
        }


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
    ("max_attempts", "job_state", "disposition"),
    [(3, "pending", "retry"), (1, "failed", "failed_final")],
)
def test_deep_json_failure_is_terminal_private_and_visible_in_status(
    worker_store: Store,
    stored: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    max_attempts: int,
    job_state: str,
    disposition: str,
) -> None:
    worker_store.settings = replace(worker_store.settings, max_attempts=max_attempts)
    project = seed(worker_store, stored["report"])
    initial = process_one(worker_store, project, CheckSettings(), DiagnosticProvider())
    assert initial and initial["status"] == "done" and initial["rules"] == 1
    before = worker_store.status(project)
    with worker_store.connect() as db:
        primary_before = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("lr_events", "lr_findings", "lr_rules")
        }

    result = process_one(worker_store, project, CheckSettings(), DiagnosticProvider("deep_json"))

    assert result and result["status"] == "retry_or_failed"
    assert result["reason"] == "memory_llm_schema_invalid"
    assert result["diagnostic"]["subreason"] == "json_invalid"
    assert (
        run_memory(
            Namespace(command="memory", memory_command="status", project=project),
            replace(stored["config"], memory=worker_store.settings),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    status = json.loads(captured.out)
    attempt = latest_attempt(status, result["attempt_id"])
    assert attempt["job_state"] == job_state
    assert attempt["outcome"] == "failed"
    assert attempt["finished_at"] is not None
    assert attempt["disposition"] == disposition
    assert attempt["stage"] == "parse_schema"
    assert attempt["error"] == "memory_llm_schema_invalid"
    assert attempt["subreason"] == "json_invalid"
    assert attempt["stages"]["parse_schema"]["outcome"] == "failed"
    assert isinstance(attempt["stages"]["parse_schema"]["duration_ms"], int)
    assert {
        "error": "memory_llm_schema_invalid",
        "subreason": "json_invalid",
        "stage": "parse_schema",
        "disposition": disposition,
        "count": 1,
    } in status["attempt_diagnostics"]["failure_distribution"]
    assert status["durable_events"] == before["durable_events"]
    assert status["findings"] == before["findings"]
    assert status["rules"] == before["rules"]
    assert not contains_private_diagnostic_keys(attempt)
    with worker_store.connect() as db:
        for table, rows in primary_before.items():
            assert db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        persisted = db.execute("SELECT * FROM lr_jobs").fetchall()
        persisted += db.execute("SELECT * FROM lr_job_attempts").fetchall()
    serialized = json.dumps([result, status, persisted], ensure_ascii=False, default=str)
    assert "synthetic-depth-secret" not in serialized
    assert "synthetic-depth-secret" not in caplog.text
    assert "TOKEN=" not in serialized
    assert "RecursionError" not in serialized


def test_work_continues_queue_after_deep_json_failure(
    worker_store: Store, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = seed(worker_store, stored["report"])
    providers = iter([DiagnosticProvider("deep_json"), DiagnosticProvider()])
    monkeypatch.setattr("language_review.memory.worker.create_provider", lambda _: next(providers))

    result = work(worker_store, project, CheckSettings(), limit=2)

    assert result["status"] == "partial"
    failed, completed = result["jobs"]
    assert failed["status"] == "retry_or_failed"
    assert failed["reason"] == "memory_llm_schema_invalid"
    assert completed["status"] == "done" and completed["rules"] == 1
    assert failed["job"] != completed["job"]
    status = worker_store.status(project)
    assert latest_attempt(status, failed["attempt_id"])["outcome"] == "failed"
    assert latest_attempt(status, completed["attempt_id"])["outcome"] == "succeeded"


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


@pytest.mark.parametrize("failure_stage", ["claim", "context"])
def test_attempt_record_failure_returns_retry_result(
    worker_store: Store,
    stored: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from language_review.memory.store import MemoryFeatureError

    project = seed(worker_store, stored["report"])
    original = worker_store.record_attempt

    def fail_record(job: dict[str, Any], diagnostic: dict[str, Any], db: Any | None = None) -> None:
        if diagnostic["stage"] == failure_stage:
            raise MemoryFeatureError("memory_database_failed")
        original(job, diagnostic, db)

    monkeypatch.setattr(worker_store, "record_attempt", fail_record)

    result = process_one(worker_store, project, CheckSettings(), DiagnosticProvider())

    assert result and result["status"] == "retry_or_failed"
    assert result["reason"] == "memory_database_failed"
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["job_state"] == "pending"
    assert attempt["outcome"] == "failed"
    assert attempt["error"] == "memory_database_failed"
    if failure_stage == "context":
        assert attempt["stage"] == "context"
        assert attempt["stages"]["context"]["outcome"] == "failed"
        assert isinstance(attempt["stages"]["context"]["duration_ms"], int)


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
    assert status_has_no_private_diagnostic_keys(worker_store.status(project))


def test_out_of_range_provider_token_counts_are_omitted(
    worker_store: Store, stored: dict[str, Any]
) -> None:
    project = seed(worker_store, stored["report"])
    result = process_one(
        worker_store,
        project,
        CheckSettings(),
        DiagnosticProvider("valid", prompt_tokens=2_147_483_648),
    )

    assert result and result["status"] == "done"
    attempt = latest_attempt(worker_store.status(project), result["attempt_id"])
    assert attempt["stages"]["completion"]["token_counts"] == {"completion": 7}


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


def status_has_no_private_diagnostic_keys(status: dict[str, Any]) -> bool:
    diagnostics = status["attempt_diagnostics"]
    return all(not contains_private_diagnostic_keys(attempt) for attempt in diagnostics["recent"])


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
    assert result["rule_selection"] == {
        "received": 0,
        "selected": 0,
        "excluded_current_evidence_missing": 0,
    }
    status = worker_store.status(project)
    attempt = latest_attempt(status, result["attempt_id"])
    assert attempt["outcome"] == "succeeded"
    assert attempt["disposition"] == "completed"
    assert attempt["stages"]["post_schema"]["rule_selection"] == result["rule_selection"]
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


def test_indexing_diagnostic_failure_keeps_results_and_continues(
    worker_store: Store, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review.memory.store import MemoryFeatureError

    project = seed(worker_store, stored["report"])
    original = worker_store.record_attempt_stage
    calls = 0

    def fail_first_record(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MemoryFeatureError("memory_database_failed")
        original(*args, **kwargs)

    monkeypatch.setattr(worker_store, "record_attempt_stage", fail_first_record)

    result = work(worker_store, project, CheckSettings(), limit=2)

    assert len(result["jobs"]) == 2
    assert all(job["status"] == "done" for job in result["jobs"])
    assert calls == 2
    assert result["status"] == "partial"
    assert result["index"]["errors"] == [{"project": project, "reason": "memory_database_failed"}]


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
