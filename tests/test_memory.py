from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import JsonServer, RecordedRequest
from support.check_helpers import _config, _run

from language_review.config import load_config
from language_review.errors import ConfigurationError, InputError
from language_review.memory.cli import submit_decision
from language_review.memory.safety import safe_text, sanitize
from language_review.memory.search import Embedder
from language_review.memory.settings import MemorySettings, validate_memory
from language_review.memory.store import MemoryError, Store


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
    with pytest.raises(InputError, match="контракт"):
        submit_decision(Store(MemorySettings()), {"status": "model_confirmed"})


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
    with pytest.raises(MemoryError, match="schema"):
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
    with pytest.raises(MemoryError, match="revision_mismatch"):
        Embedder(Store(replace(settings, embedding_revision="b" * 64))).embed(["Текст"])
