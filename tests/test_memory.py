from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import JsonServer, RecordedRequest
from support.check_helpers import _config, _run

from language_review.check_models import CheckUnit
from language_review.config import CheckSettings, load_config
from language_review.engine_llm import LlmEngine
from language_review.errors import ConfigurationError, InputError
from language_review.extract_models import ExtractDocument
from language_review.masking import EngineView
from language_review.memory.cli import run as run_memory
from language_review.memory.cli import submit_decision
from language_review.memory.pipeline import MemorySession
from language_review.memory.safety import safe_text, sanitize
from language_review.memory.search import Embedder, RecallRequest, _recall_query, recall_many
from language_review.memory.settings import MemorySettings, validate_memory
from language_review.memory.store import MemoryFeatureError, Store
from language_review.memory.worker import _post_schema_subreason, worker_lease_seconds


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": "true"},
        {"max_attempts": True},
        {"context_chars": 1},
        {"embedding_endpoint": "https://example.org"},
        {"embedding_endpoint": "http://user:password@localhost:11434"},
        {"embedding_endpoint": "http://127.0.0.1/?token=private"},
        {"embedding_revision": "latest"},
        {"dsn_env": "PASSWORD=value"},
        {"projects": {"fixture": str(uuid4())}},
        {"unknown": 1},
    ],
)
def test_memory_configuration_rejects_unsafe_or_ambiguous_settings(raw: dict) -> None:
    with pytest.raises(ConfigurationError):
        validate_memory(raw)


def test_memory_config_merges_and_masks_project_identity(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    project = str(uuid4())
    config = load_config(
        _config(tmp_path, f'[memory]\nenabled=true\n[memory.projects]\nroot-0001="{project}"\n')
    )
    assert config.memory.projects == {"root-0001": project}
    mapping = config.as_mapping()
    assert mapping["memory"]["enabled"]["value"] is True
    assert project not in json.dumps(mapping)
    assert mapping["memory"]["enabled"]["source"].startswith("конфигурация")


@pytest.mark.parametrize(
    "secret",
    [
        "password=canary",
        "api_key: private",
        "Bearer secret-value",
        "ghp_" + "x" * 30,
        "https://username:password@localhost/api",
        r"C:\Users\private\file",
        "/home/private/key",
    ],
)
def test_memory_removes_sensitive_scalars_before_persistence_and_embedding(secret: str) -> None:
    assert safe_text(secret) == "[REDACTED]"
    assert sanitize({"context": [secret]}) == {"context": ["[REDACTED]"]}
    assert safe_text("CLI HTTP JSON и UTF-16") == "CLI HTTP JSON и UTF-16"


def test_memory_disabled_never_connects_and_enabled_fails_closed(
    tmp_path: Path,
    isolated_appdata: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("Корректный русский текст.", encoding="utf-8")
    config = _config(tmp_path, '[check]\nengines=["vale"]\n')
    monkeypatch.delenv("LANGUAGE_REVIEW_MEMORY_DSN", raising=False)
    disabled = _run(
        ["--config", str(config), "check", str(source), "--no-memory", "--format", "json"],
        isolated_appdata,
    )
    assert disabled.returncode in {0, 1, 5}
    assert json.loads(disabled.stdout)["schema"] == "language-review.check"
    enabled = _run(
        [
            "--config",
            str(config),
            "check",
            str(source),
            "--memory",
            "--memory-project",
            str(uuid4()),
            "--format",
            "json",
        ],
        isolated_appdata,
    )
    assert enabled.returncode == 6
    assert enabled.stdout == ""
    assert "memory_dsn_missing" in enabled.stderr


def test_memory_decision_schema_rejects_invented_status_and_foreign_keys() -> None:
    decision = {
        "schema": "language-review.decision",
        "schema_version": "1.0",
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "item_id": "item-1",
        "decision": {
            "id": str(uuid4()),
            "expected_revision": 0,
            "status": "accepted",
            "actor": "reviewer",
            "reason": "Проверено.",
            "suggestion": None,
            "scope": "project",
            "public_rule": None,
        },
    }
    invalid_status = json.loads(json.dumps(decision))
    invalid_status["decision"]["status"] = "model_confirmed"
    with pytest.raises(InputError, match="контракт"):
        submit_decision(Store(MemorySettings()), invalid_status)

    foreign_key = json.loads(json.dumps(decision))
    foreign_key["foreign_key"] = "unexpected"
    with pytest.raises(InputError, match="контракт"):
        submit_decision(Store(MemorySettings()), foreign_key)


def test_memory_health_reports_migration_required_when_migration_table_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def fetchone(self) -> dict[str, bool]:
            return {"present": False}

    class Connection:
        def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    @contextmanager
    def connect(_store: Store) -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(Store, "connect", connect)
    with pytest.raises(MemoryFeatureError, match="memory_migration_required"):
        Store(MemorySettings()).health()


def test_memory_rejects_ambiguous_project_bindings_before_database_access() -> None:
    project = str(uuid4())
    with pytest.raises(MemoryFeatureError, match="memory_project_binding_ambiguous"):
        Store(MemorySettings()).begin(
            str(uuid4()),
            {"root-0001": project, "root-0002": project},
            "operational",
            "live",
        )


def test_memory_session_rejects_duplicate_project_uuid_before_health() -> None:
    project = str(uuid4())
    settings = MemorySettings(
        enabled=True,
        projects={"root-0001": project, "root-0002": project},
    )
    extract = SimpleNamespace(roots=[{"id": "root-0001"}, {"id": "root-0002"}], documents=[])
    with pytest.raises(MemoryFeatureError, match="memory_project_binding_ambiguous"):
        MemorySession.open(settings, extract, {}, policies=[])


def test_memory_run_rejects_invalid_uuid_before_database_access(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    config = load_config(_config(tmp_path, ""))
    arguments = Namespace(
        command="memory",
        memory_command="run",
        project=str(uuid4()),
        run_id="not-a-uuid",
    )
    with pytest.raises(InputError, match="run_id должен быть UUID"):
        run_memory(arguments, config)


def test_recall_skips_unscored_candidates_and_uses_sql_vector_match() -> None:
    project = str(uuid4())
    rows = [
        {
            "id": "unranked",
            "lexical": 0.0,
            "semantic": None,
            "vector_match": False,
            "text": "beta",
            "project_id": project,
            "state": "accepted",
            "kind": "code",
            "category": "style",
            "payload": {"evidence": []},
            "decision_id": None,
            "revision": 1,
        },
        {
            "id": "vector-candidate",
            "lexical": 0.0,
            "semantic": 0.6,
            "vector_match": True,
            "text": "gamma",
            "project_id": project,
            "state": "accepted",
            "kind": "code",
            "category": "style",
            "payload": {"evidence": []},
            "decision_id": None,
            "revision": 1,
        },
    ]

    class Result:
        def fetchall(self) -> list[dict]:
            return rows

    class Connection:
        parameters: dict[str, object] = {}

        def execute(self, _query: str, parameters: dict[str, object]) -> Result:
            self.parameters = parameters
            return Result()

    db = Connection()
    result = _recall_query(
        Store(MemorySettings()),
        db,
        RecallRequest(project=project, text="alpha", hybrid=True),
        "alpha",
        "[0.1,0.2]",
        None,
    )
    assert db.parameters["max_vector_distance"] == 0.4
    assert [entry["id"] for entry in result["results"]] == ["vector-candidate"]


@pytest.mark.parametrize(
    ("evidence", "aliases", "permitted", "rule_category", "category", "expected"),
    [
        (
            ["c9"],
            {"c1": "finding-1"},
            {"finding-1"},
            "typo",
            "typo",
            "evidence_alias_unknown",
        ),
        (
            ["c2"],
            {"c1": "finding-1", "c2": "outside-permitted"},
            {"finding-1"},
            "typo",
            "typo",
            "evidence_outside_permitted",
        ),
        (
            ["c1"],
            {"c1": "finding-1"},
            {"finding-1"},
            "other-category",
            "typo",
            "category_mismatch",
        ),
    ],
)
def test_post_schema_defensive_subreasons(
    evidence: list[str],
    aliases: dict[str, str],
    permitted: set[str],
    rule_category: str,
    category: str,
    expected: str,
) -> None:
    rule = {"evidence": evidence, "category": rule_category, "text": "праверить"}

    assert _post_schema_subreason(rule, aliases, permitted, "finding-1", category) == expected


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [(180.0, 220), (3600.0, 3640), (5.0, 40)],
)
def test_worker_lease_covers_probe_and_configured_llm_timeout(
    timeout: float, expected: int
) -> None:
    assert worker_lease_seconds(CheckSettings(llm_timeout_seconds=timeout)) == expected


def test_llm_batching_budgets_context_and_uses_larger_context_chunks() -> None:
    document = ExtractDocument(
        id="doc-1",
        root="root",
        path="guide.md",
        language="ru",
        format="markdown",
        profile=None,
        status="ok",
    )

    def unit(index: int, text: str, context_chars: int = 900) -> CheckUnit:
        return CheckUnit(
            index=index,
            document_index=0,
            document=document,
            fragments=(),
            fragment_ids=(),
            kind="paragraph",
            context="",
            sentence_start=True,
            views={"llm": EngineView(text, ())},
            engines=("llm",),
            terms={},
            memory_context=[{"id": "historical", "text": "x" * context_chars}],
        )

    engine = LlmEngine(CheckSettings(llm_batch_chars=1500))
    pair = list(engine._batches([unit(1, "a" * 500), unit(2, "b" * 500)]))
    assert len(pair) == 1 and len(pair[0]) == 2

    bounded = list(engine._batches([unit(3, "a" * 500, 4000), unit(4, "b" * 500, 4000)]))
    assert len(bounded) == 2 and all(len(batch) == 1 for batch in bounded)

    split_candidate = unit(5, "Предложение. " * 180, 4000)
    chunks = list(engine._batches([split_candidate]))
    assert len(chunks) == 1 and len(chunks[0]) == 1


def test_worker_cli_requires_one_explicit_scope(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LANGUAGE_REVIEW_MEMORY_DSN", raising=False)
    config = _config(tmp_path, "")
    command = ["--config", str(config), "memory", "work"]
    for scope in [[], ["--all-projects", "--project", str(uuid4())]]:
        result = _run([*command, *scope], isolated_appdata)
        assert result.returncode == 2
        assert result.stdout == ""
    result = _run([*command, "--all-projects"], isolated_appdata)
    assert result.returncode == 6
    assert "memory_dsn_missing" in result.stderr
    assert result.stdout == ""


def test_watch_retries_temporary_database_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_config(_config(tmp_path, ""))
    arguments = Namespace(
        command="memory", memory_command="work", project=str(uuid4()), limit=1, watch=True
    )
    health_calls = 0
    work_results = iter([MemoryFeatureError("memory_database_failed"), {"status": "processed"}])

    def health(_: Store) -> dict:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise MemoryFeatureError("memory_database_failed")
        return {"status": "ok"}

    def do_work(*_: object, **__: object) -> dict:
        result = next(work_results)
        if isinstance(result, MemoryFeatureError):
            raise result
        return result

    sleep_calls = 0

    class StopWatch(Exception):
        pass

    def stop_after_recovery(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            raise StopWatch

    monkeypatch.setattr(Store, "health", health)
    monkeypatch.setattr("language_review.memory.worker.work", do_work)
    monkeypatch.setattr("language_review.memory.cli.time.sleep", stop_after_recovery)

    with pytest.raises(StopWatch):
        run_memory(arguments, config)

    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [result["status"] for result in results] == ["waiting", "waiting", "processed"]
    assert health_calls == 3


def test_watch_does_not_retry_permanent_worker_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path, ""))
    arguments = Namespace(
        command="memory", memory_command="work", project=str(uuid4()), limit=0, watch=True
    )
    monkeypatch.setattr(Store, "health", lambda _: {"status": "ok"})

    with pytest.raises(MemoryFeatureError, match="memory_worker_limit_invalid"):
        run_memory(arguments, config)


def test_memory_session_batches_unique_recall_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(MemorySettings())
    session = MemorySession(store, "run", {"root": str(uuid4())}, "operational")

    def unit(index: int, document_id: str, text: str) -> CheckUnit:
        document = ExtractDocument(
            id=document_id,
            root="root",
            path=f"{document_id}.md",
            language="ru",
            format="markdown",
            profile=None,
            status="ok",
        )
        return CheckUnit(
            index=index,
            document_index=0,
            document=document,
            fragments=(),
            fragment_ids=(),
            kind="code",
            context="",
            sentence_start=True,
            views={"llm": EngineView(text, ())},
            engines=("llm",),
            terms={},
        )

    first = unit(1, "document-1", "общий запрос")
    duplicate = unit(2, "document-1", "общий запрос")
    distinct = unit(3, "document-2", "другой запрос")
    captured: list[RecallRequest] = []

    def recall_batch(_: Store, requests: list[RecallRequest]) -> list[dict]:
        captured.extend(requests)
        return [
            {"results": [{"id": str(index)}], "mode": "structured", "reason": None}
            for index, _ in enumerate(requests, 1)
        ]

    monkeypatch.setattr("language_review.memory.search.recall_many", recall_batch)
    session.prepare([first, duplicate, distinct])

    assert len(captured) == 2
    assert first.memory_context == duplicate.memory_context == [{"id": "1"}]
    assert distinct.memory_context == [{"id": "2"}]
    assert len(session.diagnostics["queries"]) == 3


def test_recall_many_batches_embeddings_and_reuses_database_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(MemorySettings())
    embedding_calls: list[tuple[list[str], bool]] = []
    connections = 0
    queries = 0

    class EmptyResult:
        def fetchall(self) -> list[dict]:
            return []

    class EmptyConnection:
        def execute(self, *_: object, **__: object) -> EmptyResult:
            nonlocal queries
            queries += 1
            return EmptyResult()

    connection = EmptyConnection()

    @contextmanager
    def connect() -> Iterator[EmptyConnection]:
        nonlocal connections
        connections += 1
        yield connection

    def embed(_: Embedder, texts: list[str], *, query: bool = False) -> list[list[float]]:
        embedding_calls.append((texts, query))
        return [[0.25, 0.75] for _ in texts]

    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setattr(Embedder, "embed", embed)
    requests = [
        RecallRequest(project=str(uuid4()), text="общий запрос", kind="code"),
        RecallRequest(project=str(uuid4()), text="другой запрос", kind="documentation"),
        RecallRequest(project=str(uuid4()), text="общий запрос", kind="api"),
    ]

    results = recall_many(store, requests)

    assert len(results) == 3
    assert embedding_calls == [(["общий запрос", "другой запрос"], True)]
    assert connections == 1
    assert queries == 3
    assert all(result["mode"] == "hybrid" for result in results)


def test_recall_many_falls_back_after_embedding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(MemorySettings())
    embedding_calls = 0

    class EmptyResult:
        def fetchall(self) -> list[dict]:
            return []

    class EmptyConnection:
        def execute(self, *_: object, **__: object) -> EmptyResult:
            return EmptyResult()

    connection = EmptyConnection()

    @contextmanager
    def connect() -> Iterator[EmptyConnection]:
        yield connection

    def fail_embedding(_: Embedder, __: list[str], *, query: bool = False) -> list[list[float]]:
        nonlocal embedding_calls
        embedding_calls += 1
        raise MemoryFeatureError("memory_embedding_timeout")

    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setattr(Embedder, "embed", fail_embedding)
    results = recall_many(
        store,
        [
            RecallRequest(project=str(uuid4()), text="первый запрос"),
            RecallRequest(project=str(uuid4()), text="второй запрос"),
        ],
    )

    assert embedding_calls == 1
    assert [result["mode"] for result in results] == ["structured", "structured"]
    assert [result["reason"] for result in results] == [
        "memory_embedding_timeout",
        "memory_embedding_timeout",
    ]


@pytest.mark.parametrize("vector", [[], [0, 0], [1], [True, 1], ["1", "2"]])
def test_embedding_schema_does_not_mark_malformed_vectors_indexed(
    vector: list,
    json_server: JsonServer,
) -> None:
    revision = "a" * 64

    def respond(request: RecordedRequest) -> tuple[int, object]:
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "embeddinggemma:300m", "digest": revision}]}
        return 200, {"embeddings": [vector]}

    json_server.responder = respond
    store = Store(
        replace(
            MemorySettings(),
            embedding_endpoint=json_server.url,
            embedding_model="embeddinggemma:300m",
            embedding_revision=revision,
            embedding_dimensions=2,
        )
    )
    with pytest.raises(MemoryFeatureError, match="schema"):
        Embedder(store).embed(["Русский технический текст"])


def test_embedding_pins_model_and_never_sends_secret_query(json_server: JsonServer) -> None:
    revision = "a" * 64

    def respond(request: RecordedRequest) -> tuple[int, object]:
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "embeddinggemma:300m", "digest": revision}]}
        assert request.body["input"] == ["task: search result | query: [REDACTED]"]
        return 200, {"embeddings": [[0.2, 0.8]]}

    json_server.responder = respond
    settings = replace(
        MemorySettings(),
        embedding_endpoint=json_server.url,
        embedding_model="embeddinggemma:300m",
        embedding_revision=revision,
        embedding_dimensions=2,
    )
    assert Embedder(Store(settings)).embed(["password=canary"], query=True) == [[0.2, 0.8]]
    with pytest.raises(MemoryFeatureError, match="revision_mismatch"):
        Embedder(Store(replace(settings, embedding_revision="b" * 64))).embed(["Текст"])
