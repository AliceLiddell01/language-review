"""Настоящая контейнерная БД; отдельные проекты на тест, никакой подмены storage.

LANGUAGE_REVIEW_MEMORY_TEST_DSN задаёт только изолированную приёмочную БД.
LLM/embedding live включаются отдельно; протокольные ответы не считаются live LLM.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from conftest import JsonServer
from support.check_helpers import _languagetool_config, languagetool_responder
from support.project_inputs import check_fixture_paths as check_paths
from test_analysis import Provider, finding
from test_project_config import project

from language_review.check_models import ProbeResult
from language_review.config import CheckSettings, load_config
from language_review.engine_llm import LlmEngine, ProviderReply
from language_review.errors import InputError
from language_review.memory.cli import run as run_memory
from language_review.memory.cli import submit_decision
from language_review.memory.pipeline import import_review
from language_review.memory.search import index_rules, recall
from language_review.memory.settings import MemorySettings
from language_review.memory.store import MemoryFeatureError, Store
from language_review.memory.worker import process_one
from language_review.review_report import review_document

pytestmark = pytest.mark.skipif(
    not os.environ.get("LANGUAGE_REVIEW_MEMORY_TEST_DSN"),
    reason="NOT_RUN: изолированная PostgreSQL БД не задана",
)


@pytest.fixture
def stored(tmp_path: Path, isolated_appdata: Path, json_server: JsonServer) -> dict[str, Any]:
    json_server.responder = languagetool_responder({"праверить": ["проверить"]})
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.rs").write_bytes("// 😀 Нужно праверить CLI.\r\n".encode("utf-8-sig"))
    (source / "b.rs").write_bytes("// Нужно праверить CLI.\r\n".encode("utf-16"))
    project = str(uuid4())
    settings = MemorySettings(
        enabled=True,
        dsn_env="LANGUAGE_REVIEW_MEMORY_TEST_DSN",
        projects={"root-0001": project},
        recall_mode="structured",
    )
    store = Store(settings)
    store.migrate()
    config = replace(load_config(_languagetool_config(tmp_path, json_server.url)), memory=settings)
    result = check_paths([str(source)], config)
    from language_review.project_config import load_project

    project = load_project(config, source).project_id
    report = review_document(result)
    assert result.memory["status"] == "committed"
    assert len(report["items"]) == 2
    return {
        "store": store,
        "project": project,
        "result": result,
        "report": report,
        "source": source,
        "config": config,
        "run": result.memory["run_id"],
    }


@pytest.fixture
def version_two_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Создаёт отдельную БД с миграциями 001/002 для проверки обновления до версии 003."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from language_review.memory.store import _migration_scripts

    database_name = "lr_memory_v2_" + uuid4().hex
    with psycopg.connect(os.environ["LANGUAGE_REVIEW_MEMORY_TEST_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        try:
            dsn_env = "LANGUAGE_REVIEW_MEMORY_V2_TEST_DSN"
            monkeypatch.setenv(
                dsn_env,
                make_conninfo(os.environ["LANGUAGE_REVIEW_MEMORY_TEST_DSN"], dbname=database_name),
            )
            store = Store(MemorySettings(enabled=True, dsn_env=dsn_env))
            migrations = _migration_scripts()
            assert len(migrations) >= 3
            project = str(uuid4())
            with store.connect() as db:
                db.execute(
                    "CREATE TABLE lr_migrations (version integer PRIMARY KEY, "
                    "checksum text NOT NULL, applied_at timestamptz DEFAULT clock_timestamp())"
                )
                for version, script, checksum in migrations[:2]:
                    db.execute(script)
                    db.execute(
                        "INSERT INTO lr_migrations(version,checksum) VALUES (%s,%s)",
                        (version, checksum),
                    )
                db.execute("INSERT INTO lr_projects(id) VALUES (%s)", (project,))
                db.execute(
                    "INSERT INTO lr_findings(id,project_id,identity,payload,dataset) "
                    "VALUES (%s,%s,'{}','{}','synthetic')",
                    ("legacy-finding", project),
                )
                db.execute(
                    "INSERT INTO lr_jobs(id,finding_id,revision) VALUES (%s,%s,0)",
                    ("legacy-job", "legacy-finding"),
                )
            yield {"store": store, "project": project}
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def decision(data: dict[str, Any], status: str = "accepted", revision: int = 0) -> dict[str, Any]:
    return {
        "schema": "language-review.decision",
        "schema_version": "1.0",
        "project_id": data["project"],
        "run_id": data["run"],
        "item_id": data["report"]["items"][0]["id"],
        "decision": {
            "id": str(uuid4()),
            "expected_revision": revision,
            "status": status,
            "actor": "synthetic-reviewer",
            "reason": "Проверена опечатка в русском слове.",
            "suggestion": 0 if status == "accepted" else None,
            "scope": "project",
            "public_rule": None,
        },
    }


def test_auto_ingestion_keeps_utf8_utf16_documents_evidence_and_distinct_locations(
    stored: dict,
) -> None:
    store = stored["store"]
    with store.connect() as db:
        docs = db.execute(
            "SELECT payload FROM lr_documents WHERE run_id=%s ORDER BY id", (stored["run"],)
        ).fetchall()
        observations = db.execute(
            "SELECT e.* FROM lr_events e WHERE e.run_id=%s AND kind='observation'", (stored["run"],)
        ).fetchall()
        stored_report = db.execute(
            "SELECT report FROM lr_runs WHERE id=%s", (stored["run"],)
        ).fetchone()
    assert {row["payload"]["encoding"] for row in docs} == {"utf-8", "utf-16-le"}
    assert len({row["finding_id"] for row in observations}) == 2
    assert all(
        row["payload"]["evidence"][0]["source_excerpt"] == "праверить" for row in observations
    )
    assert stored_report["report"] == stored["report"]
    assert {row["state"] for row in store.status(stored["project"])["findings"]} == {"unverified"}


def test_replay_new_runs_and_concurrent_delivery_never_duplicate_findings_or_jobs(
    stored: dict,
) -> None:
    store = stored["store"]

    def deliver(_: int) -> dict:
        return store.ingest(stored["run"], stored["report"], {})

    with ThreadPoolExecutor(max_workers=4) as pool:
        replayed = list(pool.map(deliver, range(8)))
    assert all(row["duplicate"] for row in replayed)
    second = check_paths([str(stored["source"])], stored["config"])
    assert second.memory["run_id"] != stored["run"]
    status = store.status(stored["project"])
    assert sum(row["count"] for row in status["findings"]) == 2
    assert sum(row["count"] for row in status["jobs"]) == 2
    assert sum(row["count"] for row in status["runs"]) == 2


def test_llm_finding_identity_ignores_run_diagnostics_and_neighbours(
    stored: dict, tmp_path: Path
) -> None:
    root = project(
        tmp_path / "llm",
        """schema_version: 1
default_profile: local
profiles:
  local:
    engines: [llm]
    analysis: {mode: deep, strategies: [technical]}
""",
    )
    (root / ".git").mkdir()
    (root / "z.md").write_text("Тут ошыбка.", encoding="utf-8")
    provider = Provider(
        lambda _system, payload: {
            "findings": [
                finding(item["id"]) for item in payload["items"] if "ошыбка" in item["text"]
            ]
        }
    )
    config = replace(load_config(), memory=stored["store"].settings)
    store = stored["store"]

    def run() -> Any:
        return check_paths(
            [str(root)],
            config,
            engine_factory=lambda _name, settings: LlmEngine(settings, provider),
        )

    first = run()
    (root / "a.md").write_text("Верный соседний документ.", encoding="utf-8")
    second = run()
    reports = [review_document(value) for value in (first, second)]
    assert all(len(report["items"]) == 1 for report in reports)
    assert (
        reports[0]["analysis"]["engines"][0]["details"]["analysis"]
        != reports[1]["analysis"]["engines"][0]["details"]["analysis"]
    )
    assert len(reports[0]["analysis"]["engines"][0]["details"]["document_ids"]) == 1
    assert len(reports[1]["analysis"]["engines"][0]["details"]["document_ids"]) == 2

    def finding_ids(run_id: str) -> list[str]:
        with store.connect() as db:
            return [
                row["finding_id"]
                for row in db.execute(
                    "SELECT finding_id FROM lr_occurrences WHERE run_id=%s", (run_id,)
                ).fetchall()
            ]

    assert finding_ids(first.memory["run_id"]) == finding_ids(second.memory["run_id"])
    report = deepcopy(reports[1])
    details = report["analysis"]["engines"][0]["details"]
    details["analysis"] = {"passes": [{"elapsed_ms": 9876, "units": [999]}], "requests": 99}
    details["future_diagnostic"] = {"request_id": str(uuid4())}
    details["available_models"] = ["unrelated-model"]
    run_id = str(uuid4())
    with store.connect() as db:
        original_run = db.execute(
            "SELECT * FROM lr_runs WHERE id=%s", (first.memory["run_id"],)
        ).fetchone()
    store.begin(run_id, original_run["bindings"], original_run["dataset"], "live")
    diagnostics = deepcopy(original_run["diagnostics"])
    assert store.ingest(run_id, report, diagnostics)["duplicate"] is False
    assert store.ingest(run_id, report, diagnostics)["duplicate"] is True
    assert finding_ids(first.memory["run_id"]) == finding_ids(run_id)
    with store.connect() as db:
        saved = db.execute("SELECT report FROM lr_runs WHERE id=%s", (run_id,)).fetchone()["report"]
        count = db.execute(
            "SELECT count(*) AS n FROM lr_findings WHERE project_id=%s",
            (next(iter(original_run["bindings"].values())),),
        ).fetchone()["n"]
    assert (
        saved["analysis"]["engines"][0]["details"]["future_diagnostic"]
        == details["future_diagnostic"]
    )
    assert count == 1
    policy_path = root / ".language-review.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8")
        + "prompts:\n  primary: {text: 'Изменённая существенная инструкция.'}\n",
        encoding="utf-8",
    )
    changed_policy = run()
    assert finding_ids(changed_policy.memory["run_id"]) != finding_ids(first.memory["run_id"])


@pytest.mark.parametrize("change", ["project", "source", "evidence", "policy", "engine"])
def test_finding_identity_preserves_evidence_and_policy_boundaries(
    stored: dict, change: str
) -> None:
    store = stored["store"]
    report = deepcopy(stored["report"])
    bindings = {"root-0001": str(uuid4()) if change == "project" else stored["project"]}
    if change == "source":
        for document in report["analysis"]["documents"]:
            document["source_sha256"] = "0" * 64
    elif change == "evidence":
        for item in report["items"]:
            item["evidence"][0]["rule_id"] += ":different-rule"
    elif change == "engine":
        report["analysis"]["engines"][0]["details"]["version"] = "different-version"
    with store.connect() as db:
        diagnostics = deepcopy(
            db.execute("SELECT diagnostics FROM lr_runs WHERE id=%s", (stored["run"],)).fetchone()[
                "diagnostics"
            ]
        )
    if change == "policy":
        diagnostics["policy_sha256"] = {
            doc["id"]: "1" * 64 for doc in report["analysis"]["documents"]
        }
    run_id = str(uuid4())
    store.begin(run_id, bindings, "operational", "live")
    store.ingest(run_id, report, diagnostics)
    with store.connect() as db:
        first = db.execute(
            "SELECT finding_id FROM lr_occurrences WHERE run_id=%s", (stored["run"],)
        ).fetchall()
        second = db.execute(
            "SELECT finding_id FROM lr_occurrences WHERE run_id=%s", (run_id,)
        ).fetchall()
    assert len(first) == len(second) == 2
    assert {row["finding_id"] for row in first}.isdisjoint(row["finding_id"] for row in second)


def test_explicit_decisions_are_idempotent_revisioned_and_negative_cases_remain_searchable(
    stored: dict,
) -> None:
    store = stored["store"]
    first = decision(stored)
    assert submit_decision(store, first)["revision"] == 1
    assert submit_decision(store, first)["duplicate"] is True
    stale = decision(stored, "disputed")
    with pytest.raises(MemoryFeatureError, match="revision_conflict"):
        submit_decision(store, stale)
    for revision, state in [(1, "rejected"), (2, "disputed")]:
        assert submit_decision(store, decision(stored, state, revision))["revision"] == revision + 1
        recalled = recall(store, stored["project"], "праверить", hybrid=False)["results"]
        assert [row["state"] for row in recalled] == [state]
    with store.connect() as db:
        history = db.execute(
            "SELECT h.* FROM lr_rule_history h JOIN lr_rules r ON r.id=h.rule_id "
            "WHERE r.project_id=%s ORDER BY h.revision",
            (stored["project"],),
        ).fetchall()
    assert [row["payload"]["state"] for row in history] == ["accepted", "rejected", "disputed"]


def test_concurrent_decisions_require_one_content_revision_and_isolate_projects(
    stored: dict,
) -> None:
    store = stored["store"]

    def decide(_: int) -> str:
        try:
            submit_decision(store, decision(stored))
            return "committed"
        except MemoryFeatureError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(decide, range(4)))
    assert outcomes.count("committed") == 1
    assert outcomes.count("memory_decision_revision_conflict") == 3
    assert not [
        row
        for row in recall(store, str(uuid4()), "праверить", hybrid=False)["results"]
        if row["scope"] == "project"
    ]
    foreign = decision(stored, revision=1)
    foreign["project_id"] = str(uuid4())
    with pytest.raises(MemoryFeatureError, match="not_in_project"):
        submit_decision(store, foreign)


def test_import_uses_existing_source_verifier_and_idempotent_provenance(
    stored: dict, tmp_path: Path
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(stored["report"], ensure_ascii=False), encoding="utf-8")
    roots = {"root-0001": stored["source"]}
    projects = {"root-0001": stored["project"]}
    imported = import_review(stored["store"], report_path, roots, projects, "synthetic")
    assert import_review(stored["store"], report_path, roots, projects, "synthetic")["duplicate"]
    assert imported["run_id"] != stored["run"]
    altered = json.loads(report_path.read_text("utf-8"))
    altered["items"][0]["excerpt"] = "чужой текст"
    report_path.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises((MemoryFeatureError, InputError)):
        import_review(stored["store"], report_path, roots, projects, "synthetic")
    report_path.write_text("{}", encoding="utf-8")
    with pytest.raises(InputError):
        import_review(stored["store"], report_path, roots, projects, "synthetic")


class ConsolidationProvider:
    name = "protocol-fixture"
    endpoint = "http://127.0.0.1"
    model = "synthetic-model"

    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode

    def probe(self) -> ProbeResult:
        return ProbeResult("available")

    def complete(self, system: str, user: str, schema: dict) -> ProviderReply:
        current = json.loads(user)["current"]
        if self.mode == "empty":
            return ProviderReply('{"rules": []}', truncated=False)
        if self.mode == "invalid":
            return ProviderReply('{"accepted": true}', truncated=False)
        if self.mode == "forged":
            current = "c2"
        if self.mode == "timeout":
            from language_review.engine_http import EngineHttpError

            raise EngineHttpError("timeout", "fixture")
        category = json.loads(user)["cases"][0]["category"]
        return ProviderReply(
            json.dumps(
                {
                    "rules": [
                        {
                            "text": "праверить — вероятная опечатка",
                            "category": category,
                            "relation": "pattern",
                            "evidence": (
                                [current]
                                if self.mode == "forged"
                                else [case["id"] for case in json.loads(user)["cases"]]
                            ),
                        }
                    ]
                }
            ),
            truncated=False,
        )


@pytest.mark.parametrize(
    "mode,code,stage,subreason",
    [
        ("invalid", "schema_invalid", "parse_schema", "schema_validation_failed"),
        ("forged", "evidence_invalid", "post_schema", "current_evidence_missing"),
        ("timeout", "timeout", "completion", None),
    ],
)
def test_llm_failure_preserves_events_and_observable_retry(
    stored: dict, mode: str, code: str, stage: str, subreason: str | None
) -> None:
    result = process_one(
        stored["store"], stored["project"], CheckSettings(), ConsolidationProvider(mode)
    )
    assert result and result["status"] == "retry_or_failed" and code in result["reason"]
    status = stored["store"].status(stored["project"])
    assert status["durable_events"] == 5
    assert sum(row["count"] for row in status["findings"]) == 2
    assert status["rules"] == []
    assert status["recent_errors"]
    [attempt] = [
        row
        for row in status["attempt_diagnostics"]["recent"]
        if row["attempt_id"] == result["attempt_id"]
    ]
    assert attempt["error"] == f"memory_llm_{code}"
    assert attempt["stage"] == stage
    assert attempt["subreason"] == subreason
    assert attempt["outcome"] == "failed"
    assert attempt["disposition"] == "retry"
    assert attempt["stages"][stage]["outcome"] == "failed"
    assert "duration_ms" in attempt["stages"][stage]
    assert any(
        row
        == {
            "error": f"memory_llm_{code}",
            "subreason": subreason,
            "stage": stage,
            "disposition": "retry",
            "count": 1,
        }
        for row in status["attempt_diagnostics"]["failure_distribution"]
    )


def test_status_cli_exposes_redacted_post_schema_diagnostics_only(
    stored: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "api_key=synthetic-secret"
    private_path = r"C:\Users\Private\sample.txt"

    class SensitiveProvider(ConsolidationProvider):
        def complete(self, system: str, user: str, schema: dict) -> ProviderReply:
            reply = super().complete(system, user, schema)
            document = json.loads(reply.content)
            document["rules"][0]["text"] = f"{secret} {private_path}"
            return ProviderReply(
                json.dumps(document, ensure_ascii=False),
                truncated=False,
                prompt_tokens=12,
                completion_tokens=5,
            )

    result = process_one(stored["store"], stored["project"], CheckSettings(), SensitiveProvider())
    assert result and result["status"] == "retry_or_failed"
    assert result["reason"] == "memory_llm_evidence_invalid"

    assert (
        run_memory(
            Namespace(
                command="memory",
                memory_command="status",
                project=stored["project"],
            ),
            stored["config"],
        )
        == 0
    )
    output = capsys.readouterr().out
    status = json.loads(output)
    [attempt] = [
        row
        for row in status["attempt_diagnostics"]["recent"]
        if row["attempt_id"] == result["attempt_id"]
    ]
    assert attempt["error"] == "memory_llm_evidence_invalid"
    assert attempt["subreason"] == "sanitize_changed"
    assert attempt["stages"]["completion"]["token_counts"] == {
        "prompt": 12,
        "completion": 5,
    }
    assert secret not in output
    assert private_path not in output
    assert "sanitize_changed" in output


def test_worker_accepts_empty_rules_as_a_successful_noop(stored: dict) -> None:
    result = process_one(
        stored["store"], stored["project"], CheckSettings(), ConsolidationProvider("empty")
    )
    assert result and result["status"] == "done" and result["rules"] == 0
    status = stored["store"].status(stored["project"])
    assert status["rules"] == []
    assert not status["recent_errors"]
    with stored["store"].connect() as db:
        job = db.execute("SELECT state FROM lr_jobs WHERE id=%s", (result["job"],)).fetchone()
    assert job == {"state": "done"}


def test_worker_fencing_recovery_and_hypotheses_never_promote_observations(stored: dict) -> None:
    store = stored["store"]
    lost = store.claim(stored["project"])
    assert lost
    with store.connect() as db:
        db.execute(
            "UPDATE lr_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (lost["id"],),
        )
    recovered = store.claim(stored["project"])
    assert recovered and recovered["id"] == lost["id"] and recovered["owner"] != lost["owner"]
    store.fail(lost, "stale_worker")
    with store.connect() as db:
        row = db.execute("SELECT state,owner FROM lr_jobs WHERE id=%s", (lost["id"],)).fetchone()
    assert row["state"] == "running" and row["owner"] == recovered["owner"]
    store.fail(recovered, "controlled_retry")
    assert not _hypotheses(store, stored["project"])
    with store.connect() as db:
        db.execute(
            "UPDATE lr_jobs SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (lost["id"],),
        )
    result = process_one(store, stored["project"], CheckSettings(), ConsolidationProvider())
    assert result and result["status"] == "done"
    attempts = [
        row
        for row in store.status(stored["project"])["attempt_diagnostics"]["recent"]
        if row["job_id"] == lost["id"]
    ]
    attempts.sort(key=lambda row: row["attempt"])
    assert [row["attempt"] for row in attempts] == [1, 2, 3]
    assert [row["outcome"] for row in attempts] == ["stale", "failed", "succeeded"]
    assert [row["disposition"] for row in attempts] == ["stale", "retry", "completed"]
    assert not [
        row
        for row in recall(store, stored["project"], "праверить", hybrid=False)["results"]
        if row["scope"] == "project"
    ]
    hypotheses = recall(
        store, stored["project"], "праверить", hybrid=False, include_hypotheses=True
    )["results"]
    assert [row["state"] for row in hypotheses if row["scope"] == "project"] == ["hypothesis"]
    submit_decision(store, decision(stored, "rejected"))
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) AS n FROM lr_rules WHERE project_id=%s "
                "AND kind='hypothesis' AND state='superseded'",
                (stored["project"],),
            ).fetchone()["n"]
            == 1
        )


def test_holdout_events_are_quarantined_from_worker_index_and_decisions(stored: dict) -> None:
    result = check_paths(
        [str(stored["source"])], stored["config"], memory_options={"dataset": "holdout"}
    )
    quarantined = {**stored, "run": result.memory["run_id"]}
    with pytest.raises(MemoryFeatureError, match="leakage"):
        submit_decision(stored["store"], decision(quarantined))
    with stored["store"].connect() as db:
        jobs = db.execute(
            "SELECT count(*) AS n FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s AND f.dataset='holdout'",
            (stored["project"],),
        ).fetchone()
    assert jobs["n"] == 0


def test_related_cases_form_one_incremental_hypothesis_and_retain_each_original(
    stored: dict,
) -> None:
    store = stored["store"]
    results = [
        process_one(store, stored["project"], CheckSettings(), ConsolidationProvider())
        for _ in range(2)
    ]
    assert all(result and result["status"] == "done" for result in results)
    with store.connect() as db:
        rules = db.execute(
            "SELECT * FROM lr_rules WHERE project_id=%s", (stored["project"],)
        ).fetchall()
        histories = db.execute(
            "SELECT h.* FROM lr_rule_history h JOIN lr_rules r ON r.id=h.rule_id "
            "WHERE r.project_id=%s",
            (stored["project"],),
        ).fetchall()
    assert len(rules) == 1 and len(rules[0]["payload"]["evidence"]) == 2
    assert rules[0]["state"] == "hypothesis"
    assert len(histories) == 2
    assert sum(row["count"] for row in store.status(stored["project"])["findings"]) == 2


class LineageProvider(ConsolidationProvider):
    """Группирует видимые случаи; текст зависит от их числа, чтобы проверить переиндексацию."""

    def __init__(self, group: bool = True, relation: str = "pattern", during: Any = None) -> None:
        super().__init__()
        self.group = group
        self.relation = relation
        self.during = during

    def complete(self, system: str, user: str, schema: dict) -> ProviderReply:
        if self.during:
            self.during()
        cases = json.loads(user)["cases"]
        evidence = [case["id"] for case in cases] if self.group else ["c1"]
        rule = {
            "text": f"праверить — вероятная опечатка ({len(evidence)} случ.)",
            "category": cases[0]["category"],
            "relation": self.relation,
            "evidence": evidence,
        }
        return ProviderReply(json.dumps({"rules": [rule]}, ensure_ascii=False), truncated=False)


def _embedding_store(stored: dict, json_server: JsonServer) -> Store:
    from conftest import RecordedRequest

    languagetool = languagetool_responder({"праверить": ["проверить"]})

    def respond(request: RecordedRequest) -> tuple[int, object]:
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "embeddinggemma:300m", "digest": "a" * 64}]}
        if request.path == "/api/embed":
            return 200, {"embeddings": [[1.0, 0.5] for _ in request.body["input"]]}
        return languagetool(request)

    json_server.responder = respond
    return Store(
        replace(
            stored["store"].settings,
            embedding_model="embeddinggemma:300m",
            embedding_revision="a" * 64,
            embedding_endpoint=json_server.url,
            embedding_dimensions=2,
        )
    )


def _add_case(stored: dict, name: str) -> None:
    (stored["source"] / name).write_text("// Нужно праверить CLI.\n", encoding="utf-8")
    assert check_paths([str(stored["source"])], stored["config"]).memory["status"] == "committed"


def _hypotheses(store: Store, project: str) -> list[dict[str, Any]]:
    with store.connect() as db:
        return list(
            db.execute(
                "SELECT * FROM lr_rules WHERE project_id=%s AND kind='hypothesis' ORDER BY id",
                (project,),
            ).fetchall()
        )


def test_sequential_related_cases_extend_one_lineage_with_history_recall_and_index(
    stored: dict, json_server: JsonServer
) -> None:
    store = _embedding_store(stored, json_server)
    project = stored["project"]
    for _ in range(2):
        assert process_one(store, project, CheckSettings(), LineageProvider())["status"] == "done"
    [lineage] = _hypotheses(store, project)
    assert index_rules(store, project)["indexed"] == 1
    # Новые случаи поступают после публикации обобщения, по одному.
    for count, name in [(3, "c.rs"), (4, "d.rs"), (5, "e.rs")]:
        _add_case(stored, name)
        outcome = process_one(store, project, CheckSettings(), LineageProvider())
        assert outcome and outcome["status"] == "done"
        assert outcome["successions"] == [
            {"rule": lineage["id"], "revision": count, "superseded": []}
        ]
        rules = _hypotheses(store, project)
        assert [row["id"] for row in rules] == [lineage["id"]]
        assert rules[0]["state"] == "hypothesis" and rules[0]["revision"] == count
        assert len(rules[0]["payload"]["evidence"]) == count
        assert index_rules(store, project)["indexed"] == 1
    with store.connect() as db:
        history = db.execute(
            "SELECT revision,payload FROM lr_rule_history WHERE rule_id=%s ORDER BY revision",
            (lineage["id"],),
        ).fetchall()
        findings = db.execute(
            "SELECT id FROM lr_findings WHERE project_id=%s", (project,)
        ).fetchall()
        embeddings = db.execute(
            "SELECT e.text_hash=md5(r.text) AS current FROM lr_embeddings e "
            "JOIN lr_rules r ON r.id=e.rule_id WHERE r.project_id=%s",
            (project,),
        ).fetchall()
    assert [row["revision"] for row in history] == [1, 2, 3, 4, 5]
    assert [len(row["payload"]["payload"]["evidence"]) for row in history] == [2, 2, 3, 4, 5]
    assert set(rules[0]["payload"]["evidence"]) == {row["id"] for row in findings}
    assert embeddings == [{"current": True}]
    status = store.status(project)
    assert status["index"] == {"rules": 1, "indexed": 1}
    assert {row["state"] for row in status["findings"]} == {"unverified"}
    recalled = recall(store, project, "праверить", include_hypotheses=True)["results"]
    hypotheses = [row for row in recalled if row["state"] == "hypothesis"]
    assert [row["id"] for row in hypotheses] == [lineage["id"]]
    assert hypotheses[0]["text"].endswith("(5 случ.)") and len(hypotheses[0]["evidence"]) == 5
    assert not [
        row for row in recall(store, project, "праверить")["results"] if row["kind"] == "hypothesis"
    ]


def test_ungrouped_and_other_relation_cases_stay_separate_until_llm_links_them(
    stored: dict, json_server: JsonServer
) -> None:
    store = _embedding_store(stored, json_server)
    project = stored["project"]
    for _ in range(2):
        process_one(store, project, CheckSettings(), LineageProvider())
    [first] = _hypotheses(store, project)
    # Та же категория и цитата без связи от LLM — не повод объединять дефекты.
    _add_case(stored, "c.rs")
    process_one(store, project, CheckSettings(), LineageProvider(group=False))
    _add_case(stored, "d.rs")
    process_one(store, project, CheckSettings(), LineageProvider(relation="terminology"))
    active = [row for row in _hypotheses(store, project) if row["state"] == "hypothesis"]
    assert len(active) == 3 and first["id"] in {row["id"] for row in active}
    single = next(row for row in active if len(row["payload"]["evidence"]) == 1)
    # Явная группировка свидетельств связывает ветви: младшая замещается, история остаётся.
    _add_case(stored, "e.rs")
    outcome = process_one(store, project, CheckSettings(), LineageProvider())
    assert outcome["successions"] == [
        {"rule": first["id"], "revision": 3, "superseded": [single["id"]]}
    ]
    rules = {row["id"]: row for row in _hypotheses(store, project)}
    assert rules[single["id"]]["state"] == "superseded"
    assert rules[single["id"]]["payload"]["superseded_by"] == first["id"]
    assert len(rules[first["id"]]["payload"]["evidence"]) == 5
    terminology = [row for row in rules.values() if row["payload"]["relation"] == "terminology"]
    assert [row["state"] for row in terminology] == ["hypothesis"]
    assert index_rules(store, project)["indexed"] == 2
    recalled = recall(store, project, "праверить", include_hypotheses=True)["results"]
    assert single["id"] not in {row["id"] for row in recalled}
    with store.connect() as db:
        merged = db.execute(
            "SELECT revision,payload FROM lr_rule_history WHERE rule_id=%s ORDER BY revision",
            (single["id"],),
        ).fetchall()
    assert [row["payload"]["state"] for row in merged] == ["hypothesis", "superseded"]


def test_decision_during_llm_never_revives_superseded_lineage(
    stored: dict, json_server: JsonServer
) -> None:
    store = _embedding_store(stored, json_server)
    project = stored["project"]
    for _ in range(2):
        process_one(store, project, CheckSettings(), LineageProvider())
    [lineage] = _hypotheses(store, project)
    _add_case(stored, "c.rs")
    rejected = LineageProvider(during=lambda: submit_decision(store, decision(stored, "rejected")))
    outcome = process_one(store, project, CheckSettings(), rejected)
    assert outcome["stale"] is True and outcome["rules"] == 0
    [attempt] = [
        row
        for row in store.status(project)["attempt_diagnostics"]["recent"]
        if row["attempt_id"] == outcome["attempt_id"]
    ]
    assert attempt["outcome"] == "stale"
    assert attempt["disposition"] == "stale"
    assert attempt["stage"] == "persistence"
    assert attempt["stages"]["persistence"]["outcome"] == "stale"
    rules = _hypotheses(store, project)
    assert [(row["id"], row["state"]) for row in rules] == [(lineage["id"], "superseded")]
    with store.connect() as db:
        case = db.execute(
            "SELECT state FROM lr_rules WHERE project_id=%s AND kind='case'", (project,)
        ).fetchall()
    assert case == [{"state": "rejected"}]
    assert not [
        row
        for row in recall(store, project, "праверить", include_hypotheses=True)["results"]
        if row["state"] == "hypothesis"
    ]
    # Повторная обработка после решения формирует свежую гипотезу, решение остаётся отрицательным.
    while (job := process_one(store, project, CheckSettings(), LineageProvider())) is not None:
        assert job["status"] == "done"
    active = [row for row in _hypotheses(store, project) if row["state"] == "hypothesis"]
    assert len(active) == 1
    states = {row["state"] for row in store.status(project)["findings"]}
    assert states == {"rejected", "unverified"}


def test_concurrent_related_jobs_keep_one_active_lineage(
    stored: dict, json_server: JsonServer
) -> None:
    store = _embedding_store(stored, json_server)
    project = stored["project"]
    for _ in range(2):
        process_one(store, project, CheckSettings(), LineageProvider())
    _add_case(stored, "c.rs")
    _add_case(stored, "d.rs")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: process_one(store, project, CheckSettings(), LineageProvider()), [0, 1]
            )
        )
    assert all(row and row["status"] == "done" for row in outcomes)
    active = [row for row in _hypotheses(store, project) if row["state"] == "hypothesis"]
    assert len(active) == 1 and len(active[0]["payload"]["evidence"]) == 4
    assert process_one(store, project, CheckSettings(), LineageProvider()) is None


def test_automatic_recall_reaches_real_llm_transport_and_keeps_protected_tokens(
    stored: dict,
    json_server: JsonServer,
) -> None:
    from support.check_helpers import llama_responder

    submit_decision(stored["store"], decision(stored, "rejected"))
    json_server.requests.clear()
    json_server.responder = llama_responder([])
    settings = replace(
        stored["config"].check, engines=("llm",), llm_llama_cpp_endpoint=json_server.url
    )
    config = replace(stored["config"], check=settings)
    (stored["source"] / "a.rs").write_text(
        "// Нужно праверить id.ToString() и {0}.\n", encoding="utf-8"
    )
    initial = {path: path.read_bytes() for path in stored["source"].glob("*.rs")}
    checked = check_paths([str(stored["source"])], config)
    requests = [
        request for request in json_server.requests if request.path == "/v1/chat/completions"
    ]
    assert requests
    sent = [
        item
        for request in requests
        for item in json.loads(request.body["messages"][1]["content"])["items"]
    ]
    contexts = [entry for item in sent for entry in item.get("memory_context", [])]
    assert contexts and all(entry["state"] == "rejected" for entry in contexts)
    assert all("ToString" not in item["text"] and "{0}" not in item["text"] for item in sent)
    assert any("⟦" in item["text"] for item in sent)
    assert checked.memory["context_units"] == 2
    assert checked.memory["status"] == "committed"
    assert initial == {path: path.read_bytes() for path in initial}
    assert "memory_context" in requests[0].body["messages"][0]["content"]


def test_retry_budget_and_recovery_are_observable(stored: dict) -> None:
    store = Store(replace(stored["store"].settings, max_attempts=1))
    failed = process_one(
        store, stored["project"], CheckSettings(), ConsolidationProvider("timeout")
    )
    assert failed and failed["status"] == "retry_or_failed"
    with store.connect() as db:
        job = db.execute(
            "SELECT j.* FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s AND j.state='failed'",
            (stored["project"],),
        ).fetchone()
    assert job["attempts"] == 1 and job["error"] == "memory_llm_timeout"
    [first] = [
        row
        for row in store.status(stored["project"])["attempt_diagnostics"]["recent"]
        if row["attempt_id"] == failed["attempt_id"]
    ]
    assert first["attempt"] == 1 and first["retry"] is False
    assert first["outcome"] == "failed" and first["disposition"] == "failed_final"
    assert store.retry(stored["project"]) == 1
    with store.connect() as db:
        restored = db.execute(
            "SELECT state,attempts FROM lr_jobs WHERE id=%s", (job["id"],)
        ).fetchone()
    assert restored == {"state": "pending", "attempts": 0}
    recovered = process_one(store, stored["project"], CheckSettings(), ConsolidationProvider())
    assert recovered and recovered["status"] == "done"
    diagnostics = store.status(stored["project"])["attempt_diagnostics"]
    history = [row for row in diagnostics["recent"] if row["job_id"] == job["id"]]
    history.sort(key=lambda row: row["attempt"])
    assert [row["attempt"] for row in history] == [1, 2]
    assert [row["retry"] for row in history] == [False, True]
    assert [row["disposition"] for row in history] == ["failed_final", "completed"]
    assert any(
        row["error"] == "memory_llm_timeout" and row["stage"] == "completion" and row["count"] == 1
        for row in diagnostics["failure_distribution"]
    )


def test_invalid_attempt_diagnostic_does_not_leave_job_running(stored: dict) -> None:
    store = stored["store"]
    job = store.claim(stored["project"])
    assert job is not None
    invalid_diagnostic = {
        "stage": "completion",
        "outcome": "running",
        "disposition": "running",
        "error": None,
        "subreason": None,
        "stages": {
            "completion": {
                "outcome": "running",
                "token_counts": {"prompt": 2_147_483_648},
            }
        },
    }

    store.fail(job, "controlled_retry", invalid_diagnostic)

    attempt = next(
        row
        for row in store.status(stored["project"])["attempt_diagnostics"]["recent"]
        if row["attempt_id"] == str(job["attempt_id"])
    )
    assert attempt["job_state"] == "pending"
    assert attempt["outcome"] == "failed"
    assert attempt["disposition"] == "retry"
    assert attempt["error"] == "controlled_retry"
    assert "token_counts" not in attempt["stages"].get("completion", {})


def test_attempt_history_is_bounded_per_job_and_keeps_monotonic_sequence(stored: dict) -> None:
    store = Store(replace(stored["store"].settings, max_attempts=1))
    with store.connect() as db:
        jobs = db.execute(
            "SELECT j.id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s ORDER BY j.created_at,j.id",
            (stored["project"],),
        ).fetchall()
        target = jobs[0]["id"]
        db.execute(
            "UPDATE lr_jobs SET state='done' WHERE id!=%s AND finding_id IN "
            "(SELECT id FROM lr_findings WHERE project_id=%s)",
            (target, stored["project"]),
        )

    for _ in range(18):
        job = store.claim(stored["project"])
        assert job and job["id"] == target
        store.fail(job, "controlled_retry")
        if job["attempts"] >= store.settings.max_attempts:
            store.retry(stored["project"])

    diagnostics = store.status(stored["project"])["attempt_diagnostics"]
    history = [row for row in diagnostics["recent"] if row["job_id"] == target]
    history.sort(key=lambda row: row["attempt"])
    assert len(history) == 16
    assert [row["attempt"] for row in history] == list(range(3, 19))
    assert all(row["retry"] is True for row in history)
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) AS count FROM lr_job_attempts WHERE job_id=%s", (target,)
            ).fetchone()["count"]
            == 16
        )


def test_schema_migration_is_idempotent_and_detects_drift(stored: dict) -> None:
    store = stored["store"]
    assert store.migrate()["schema_version"] == 3
    assert store.health()["schema_version"] == 3
    assert store.health()["pgvector"]
    # Проверка drift на отдельной транзакции без изменения общей тестовой схемы.
    from unittest.mock import patch

    class AlteredMigration:
        def joinpath(self, path: str) -> Any:
            return self

        def read_text(self, encoding: str) -> str:
            return "SELECT 'different migration';"

    with patch("language_review.memory.store.files", return_value=AlteredMigration()):
        with pytest.raises(MemoryFeatureError, match="migration_mismatch"):
            store.migrate()


def test_migration_from_version_two_preserves_existing_jobs(version_two_store: dict) -> None:
    store = version_two_store["store"]
    with pytest.raises(MemoryFeatureError, match="migration_required"):
        store.health()

    migrated = store.migrate()
    assert migrated["schema_version"] == 3
    assert store.health()["schema_version"] == 3
    with store.connect() as db:
        assert db.execute(
            "SELECT state,attempts FROM lr_jobs WHERE id=%s", ("legacy-job",)
        ).fetchone() == {"state": "pending", "attempts": 0}
        assert db.execute("SELECT version FROM lr_migrations ORDER BY version").fetchall() == [
            {"version": 1},
            {"version": 2},
            {"version": 3},
        ]
        assert db.execute("SELECT to_regclass('lr_job_attempts') AS relation").fetchone() == {
            "relation": "lr_job_attempts"
        }


def test_global_scope_requires_reviewed_public_text_and_never_exports_private_context(
    stored: dict,
) -> None:
    document = decision(stored)
    document["decision"].update(scope="global", public_rule=None)
    with pytest.raises(MemoryFeatureError, match="public_rule_required"):
        submit_decision(stored["store"], document)
    term = "Контрольныйтермин" + uuid4().hex
    document["decision"]["public_rule"] = term + " — точное техническое имя в документации."
    submit_decision(stored["store"], document)
    results = recall(stored["store"], str(uuid4()), term, hybrid=False)["results"]
    assert len(results) == 1 and results[0]["scope"] == "global"
    assert results[0]["text"] == document["decision"]["public_rule"]
    assert "synthetic-reviewer" not in json.dumps(results)
    assert "context" not in results[0] and "path" not in results[0]


def test_decide_review_cli_refuses_whitespace_global_rule_and_keeps_scope_limits(
    stored: dict, tmp_path: Path, isolated_appdata: Path
) -> None:
    from support.check_helpers import _run

    config = tmp_path / "decide.toml"
    config.write_text(
        '[memory]\nenabled=true\ndsn_env="LANGUAGE_REVIEW_MEMORY_TEST_DSN"\n', encoding="utf-8"
    )
    path = tmp_path / "decision.json"

    def decide(document: dict[str, Any]) -> Any:
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return _run(["--config", str(config), "decide-review", str(path)], isolated_appdata)

    blank = decision(stored)
    blank["decision"].update(scope="global", public_rule=" \t ")
    refused = decide(blank)
    assert refused.returncode == 2 and refused.stdout == ""
    assert "Некорректный контракт решения" in refused.stderr
    # Серверная граница отклоняет то же значение и без JSON-схемы.
    with pytest.raises(MemoryFeatureError, match="public_rule_required"):
        stored["store"].decide(
            blank["project_id"], blank["run_id"], blank["item_id"], dict(blank["decision"])
        )
    project_rule = decision(stored)
    project_rule["decision"]["public_rule"] = "Проектное исключение."
    conflict = decide(project_rule)
    assert conflict.returncode == 6 and "memory_public_rule_scope_conflict" in conflict.stderr
    assert {row["state"] for row in stored["store"].status(stored["project"])["findings"]} == {
        "unverified"
    }
    # Глобальное правило видно всем проектам общей тестовой БД: термин уникален.
    term = "Глобальныйтермин" + uuid4().hex
    meaningful = decision(stored)
    meaningful["decision"].update(scope="global", public_rule=f"{term} пишется латиницей.")
    accepted = decide(meaningful)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["revision"] == 1
    found = recall(stored["store"], str(uuid4()), term, hybrid=False)["results"]
    assert [row["text"] for row in found] == [meaningful["decision"]["public_rule"]]


def test_conflicting_delivery_rolls_back_without_erasing_original(stored: dict) -> None:
    altered = json.loads(json.dumps(stored["report"]))
    altered["items"][0]["excerpt"] = "подменённое наблюдение"
    with pytest.raises(MemoryFeatureError, match="delivery_conflict"):
        stored["store"].ingest(stored["run"], altered, {})
    with stored["store"].connect() as db:
        row = db.execute("SELECT report FROM lr_runs WHERE id=%s", (stored["run"],)).fetchone()
    assert row["report"] == stored["report"]


def test_semantic_retrieval_finds_case_without_shared_terms_in_real_pgvector(
    stored: dict,
    json_server: JsonServer,
) -> None:
    from conftest import RecordedRequest

    revision = "a" * 64

    def respond(request: RecordedRequest) -> tuple[int, object]:
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "embeddinggemma:300m", "digest": revision}]}
        return 200, {"embeddings": [[1.0, 0.0] for _ in request.body["input"]]}

    json_server.responder = respond
    settings = replace(
        stored["store"].settings,
        embedding_model="embeddinggemma:300m",
        embedding_revision=revision,
        embedding_endpoint=json_server.url,
        embedding_dimensions=2,
    )
    store = Store(settings)
    submit_decision(store, decision(stored))
    assert index_rules(store, stored["project"])["indexed"] == 1
    query = "Синонимическийзапросбезсовпадений"
    assert recall(store, stored["project"], query, hybrid=False)["results"] == []
    found = recall(store, stored["project"], query, hybrid=True)["results"]
    assert len(found) == 1 and found[0]["scope"] == "project"
    assert found[0]["semantic"] == pytest.approx(1.0)
    assert found[0]["state"] == "accepted" and found[0]["advisory_only"] is True


def test_multiple_root_bindings_keep_counters_and_identity_isolated(
    stored: dict, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    (foreign / "private.rs").write_text("// Нужно праверить текст.\n", encoding="utf-8")
    other_project = str(uuid4())
    settings = replace(
        stored["config"].memory,
        projects={"root-0001": stored["project"], "root-0002": other_project},
    )
    checked = check_paths(
        [str(stored["source"]), str(foreign)], replace(stored["config"], memory=settings)
    )
    from language_review.project_config import load_project

    other_project = load_project(stored["config"], foreign).project_id
    assert checked.memory["status"] == "committed"
    own = stored["store"].status(stored["project"])
    other = stored["store"].status(other_project)
    assert own["durable_events"] == 10 and other["durable_events"] == 3
    assert sum(row["count"] for row in own["findings"]) == 2
    assert sum(row["count"] for row in other["findings"]) == 1

    failed = process_one(
        stored["store"], stored["project"], CheckSettings(), ConsolidationProvider("timeout")
    )
    succeeded = process_one(
        stored["store"], other_project, CheckSettings(), ConsolidationProvider("empty")
    )
    assert failed and succeeded
    own_diagnostics = stored["store"].status(stored["project"])["attempt_diagnostics"]
    other_diagnostics = stored["store"].status(other_project)["attempt_diagnostics"]
    assert [row["job_id"] for row in own_diagnostics["recent"]] == [failed["job"]]
    assert [row["job_id"] for row in other_diagnostics["recent"]] == [succeeded["job"]]
    assert own_diagnostics["failure_distribution"] == [
        {
            "error": "memory_llm_timeout",
            "subreason": None,
            "stage": "completion",
            "disposition": "retry",
            "count": 1,
        }
    ]
    assert other_diagnostics["failure_distribution"] == []


def test_real_unavailable_llm_endpoint_keeps_events_and_never_reports_clean(stored: dict) -> None:
    settings = replace(
        CheckSettings(),
        engines=("llm",),
        llm_provider="ollama",
        llm_ollama_endpoint="http://127.0.0.1:9",
        llm_ollama_model="missing-model",
    )
    outcome = process_one(stored["store"], stored["project"], settings)
    assert outcome and outcome["reason"] == "memory_llm_unavailable"
    checked = check_paths([str(stored["source"])], replace(stored["config"], check=settings))
    assert checked.status == "unavailable"
    assert checked.memory["status"] == "committed"
    with stored["store"].connect() as db:
        run = db.execute(
            "SELECT report FROM lr_runs WHERE id=%s", (checked.memory["run_id"],)
        ).fetchone()
    assert run["report"]["status"] == "unavailable"
    assert all(doc["status"] == "unavailable" for doc in run["report"]["analysis"]["documents"])


@pytest.mark.parametrize(
    "text,excerpt,reason",
    [
        ("Нужно праверить и праверить.", "праверить", "excerpt_ambiguous"),
        ("Нужно проверить id.ToString().", "⟦1⟧", "targets_protected_text"),
    ],
)
def test_memory_keeps_ambiguous_and_protected_outputs_as_rejections(
    stored: dict,
    json_server: JsonServer,
    text: str,
    excerpt: str,
    reason: str,
) -> None:
    from support.check_helpers import _findings, _llm_finding, llama_responder

    for path in stored["source"].glob("*.rs"):
        path.write_text("// " + text + "\n", encoding="utf-8")
    json_server.responder = llama_responder(
        lambda items: _findings(_llm_finding(items[0]["id"], excerpt, "замена"))
    )
    config = replace(
        stored["config"],
        check=replace(
            stored["config"].check, engines=("llm",), llm_llama_cpp_endpoint=json_server.url
        ),
    )
    checked = check_paths([str(stored["source"])], config)
    report = review_document(checked)
    assert report["items"] == []
    assert reason in {row["reason"] for row in report["unlocalized"]}
    with stored["store"].connect() as db:
        events = db.execute(
            "SELECT payload FROM lr_events WHERE run_id=%s AND kind='unlocalized'",
            (checked.memory["run_id"],),
        ).fetchall()
    assert events and reason in {row["payload"]["reason"] for row in events}


@pytest.mark.skipif(
    not os.environ.get("LANGUAGE_REVIEW_MEMORY_LIVE_LLM"), reason="NOT_RUN: live LLM не задана"
)
def test_live_public_review_decision_and_automatic_next_recall(
    stored: dict, tmp_path: Path, isolated_appdata: Path
) -> None:
    from support.check_helpers import _run

    config = tmp_path / "live-memory.toml"
    config.write_text(
        '[check]\nengines=["llm"]\n[check.llm]\nprovider="ollama"\n'
        "timeout_seconds=120\nmax_output_tokens=2048\n[check.llm.ollama]\n"
        f'model="{os.environ["LANGUAGE_REVIEW_MEMORY_LIVE_LLM"]}"\n'
        '[memory]\nenabled=true\ndsn_env="LANGUAGE_REVIEW_MEMORY_TEST_DSN"\n'
        'recall_mode="structured"\n[memory.projects]\n'
        f'root-0001="{stored["project"]}"\n',
        encoding="utf-8",
    )
    command = [
        "--config",
        str(config),
        "check",
        str(stored["source"]),
        "--view",
        "review",
        "--format",
        "json",
    ]
    first = _run(command, isolated_appdata)
    assert first.returncode in {1, 4}, first.stderr
    report = json.loads(first.stdout)
    assert report["items"]
    receipt = json.loads(
        next(
            line.removeprefix("Память: ")
            for line in first.stderr.splitlines()
            if line.startswith("Память: ")
        )
    )
    event = decision({**stored, "run": receipt["run_id"], "report": report})
    event["decision"]["suggestion"] = None
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    decided = _run(["--config", str(config), "decide-review", str(path)], isolated_appdata)
    assert decided.returncode == 0, decided.stderr
    assert json.loads(decided.stdout)["revision"] == 1
    second = _run(command, isolated_appdata)
    assert second.returncode in {1, 4}, second.stderr
    recalled = json.loads(
        next(
            line.removeprefix("Память: ")
            for line in second.stderr.splitlines()
            if line.startswith("Память: ")
        )
    )
    assert recalled["context_units"] >= 1
    assert any(query["rules"] for query in recalled["queries"])
    assert recalled["run_id"] != receipt["run_id"]
    assert json.loads(second.stdout)["schema"] == "language-review.review"


@pytest.mark.skipif(
    not os.environ.get("LANGUAGE_REVIEW_MEMORY_LIVE_LLM"), reason="NOT_RUN: live LLM не задана"
)
def test_live_local_llm_consolidates_real_queue_without_promoting_truth(stored: dict) -> None:
    settings = replace(
        CheckSettings(),
        llm_provider="ollama",
        llm_ollama_model=os.environ["LANGUAGE_REVIEW_MEMORY_LIVE_LLM"],
        llm_timeout_seconds=120,
        llm_max_output_tokens=2048,
    )
    result = process_one(stored["store"], stored["project"], settings)
    assert result and result["status"] == "done", result
    assert result["rules"] > 0
    assert {row["state"] for row in stored["store"].status(stored["project"])["findings"]} == {
        "unverified"
    }


@pytest.mark.skipif(
    not os.environ.get("LANGUAGE_REVIEW_MEMORY_EMBEDDING_MODEL"),
    reason="NOT_RUN: live embeddings не заданы",
)
def test_live_pgvector_search_readback_is_scoped_and_versioned(stored: dict) -> None:
    settings = replace(
        stored["store"].settings,
        embedding_model=os.environ["LANGUAGE_REVIEW_MEMORY_EMBEDDING_MODEL"],
        embedding_revision=os.environ["LANGUAGE_REVIEW_MEMORY_EMBEDDING_REVISION"],
        embedding_dimensions=int(os.environ["LANGUAGE_REVIEW_MEMORY_EMBEDDING_DIMENSIONS"]),
    )
    store = Store(settings)
    submit_decision(store, decision(stored))
    assert index_rules(store, stored["project"])["indexed"] == 1
    assert index_rules(store, stored["project"])["indexed"] == 0
    response = recall(store, stored["project"], "праверить CLI", hybrid=True)
    assert response["mode"] == "hybrid"
    assert len(response["results"]) == 1
    assert response["results"][0]["state"] == "accepted"
    assert response["results"][0]["semantic"] is not None
    assert recall(store, str(uuid4()), "праверить CLI", hybrid=True)["results"] == []
    assert index_rules(store, stored["project"], rebuild=True)["indexed"] == 1
