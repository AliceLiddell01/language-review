"""Настоящая контейнерная БД; отдельные проекты на тест, никакой подмены storage.

LANGUAGE_REVIEW_MEMORY_TEST_DSN задаёт только изолированную приёмочную БД.
LLM/embedding live включаются отдельно; протокольные ответы не считаются live LLM.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from conftest import JsonServer
from support.check_helpers import _languagetool_config, languagetool_responder

from language_review.check import check_paths
from language_review.check_models import ProbeResult
from language_review.config import CheckSettings, load_config
from language_review.engine_llm import ProviderReply
from language_review.errors import InputError
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
    "mode,code",
    [
        ("invalid", "schema_invalid"),
        ("forged", "evidence_invalid"),
        ("timeout", "timeout"),
    ],
)
def test_llm_failure_preserves_events_and_observable_retry(
    stored: dict, mode: str, code: str
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
    with store.connect() as db:
        db.execute(
            "UPDATE lr_jobs SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (lost["id"],),
        )
    result = process_one(store, stored["project"], CheckSettings(), ConsolidationProvider())
    assert result and result["status"] == "done"
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
    assert (
        process_one(store, stored["project"], CheckSettings(), ConsolidationProvider("timeout"))[
            "status"
        ]
        == "retry_or_failed"
    )
    with store.connect() as db:
        job = db.execute(
            "SELECT j.* FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
            "WHERE f.project_id=%s AND j.state='failed'",
            (stored["project"],),
        ).fetchone()
    assert job["attempts"] == 1 and job["error"] == "memory_llm_timeout"
    assert store.retry(stored["project"]) == 1
    with store.connect() as db:
        restored = db.execute(
            "SELECT state,attempts FROM lr_jobs WHERE id=%s", (job["id"],)
        ).fetchone()
    assert restored == {"state": "pending", "attempts": 0}


def test_schema_migration_is_idempotent_and_detects_drift(stored: dict) -> None:
    store = stored["store"]
    assert store.migrate()["schema_version"] == 1
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
    assert checked.memory["status"] == "committed"
    own = stored["store"].status(stored["project"])
    other = stored["store"].status(other_project)
    assert own["durable_events"] == 10 and other["durable_events"] == 3
    assert sum(row["count"] for row in own["findings"]) == 2
    assert sum(row["count"] for row in other["findings"]) == 1


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
