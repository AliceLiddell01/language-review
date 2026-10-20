from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.check_helpers import languagetool_responder
from test_analysis import Provider, finding
from test_chatgpt import MemoryStore, install_stream, sse

from language_review.chatgpt_auth import ChatGptAuth
from language_review.chatgpt_http import CloudError
from language_review.chatgpt_store import CredentialStore, dpapi
from language_review.check import check_paths
from language_review.config import CheckSettings, load_config
from language_review.engine_chatgpt import ChatGptProvider
from language_review.engine_languagetool import LanguageToolEngine
from language_review.engine_llm import LlmEngine


@pytest.mark.parametrize("event", [{"type": []}, {"type": {}}])
def test_malformed_sse_discriminator_is_diagnostic(
    monkeypatch: pytest.MonkeyPatch, event: Any
) -> None:
    from language_review.chatgpt_http import stream_response

    install_stream(monkeypatch, sse(event))
    with pytest.raises(CloudError, match="sse_protocol_error"):
        stream_response({"model": "gpt-6-luna"}, "synthetic-access", 1)


def test_nonstring_error_parameter_does_not_escape_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from language_review.chatgpt_http import stream_response

    install_stream(
        monkeypatch,
        sse(
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "subscription_sharing_unsupported_capability",
                        "param": ["reasoning"],
                    }
                },
            }
        ),
    )
    with pytest.raises(CloudError) as caught:
        stream_response({"model": "gpt-6-luna"}, "synthetic-access", 1)
    assert caught.value.code == "subscription_sharing_unsupported_capability"
    assert caught.value.metadata()["param"] is None


def test_top_level_sse_error_preserves_quota_and_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    from language_review.chatgpt_http import stream_response

    install_stream(
        monkeypatch,
        sse(
            {
                "type": "error",
                "code": "subscription_sharing_usage_limit_exceeded",
                "param": "model",
                "message": "untrusted diagnostic",
            }
        ),
    )
    with pytest.raises(CloudError) as caught:
        stream_response({"model": "gpt-6-luna"}, "synthetic-access", 1)
    assert caught.value.code == "subscription_sharing_usage_limit_exceeded"
    assert caught.value.metadata()["param"] == "model"
    assert "untrusted diagnostic" not in str(caught.value.metadata())


@pytest.mark.skipif(os.name != "nt", reason="Windows protected storage")
@pytest.mark.parametrize(
    "content",
    [
        {"version": 1, "accounts": {}},
        {"version": 1, "accounts": {}, "active": [], "host_id": "urn:uuid:test"},
        {"version": 1, "accounts": {"one": {}}, "active": "one", "host_id": "urn:uuid:test"},
    ],
)
def test_corrupt_protected_storage_is_diagnostic(tmp_path: Path, content: Any) -> None:
    store = CredentialStore(tmp_path / "auth")
    store.directory.mkdir()
    store.path.write_bytes(dpapi(json.dumps(content).encode()))
    with pytest.raises(CloudError, match="chatgpt_storage_invalid"):
        ChatGptAuth(store).status()


@pytest.mark.skipif(os.name != "nt", reason="Windows protected storage")
def test_credentials_cannot_be_saved_in_a_repository(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    store = CredentialStore(tmp_path / "auth")
    with pytest.raises(CloudError, match="chatgpt_storage_inside_repository"):
        ChatGptAuth(store).status()
    assert not store.directory.exists()


def test_model_catalog_and_requests_pin_selected_account(monkeypatch: pytest.MonkeyPatch) -> None:
    import language_review.engine_chatgpt as engine_module

    store = MemoryStore()
    store.data["accounts"]["two"] = copy.deepcopy(store.data["accounts"]["one"]) | {
        "client_id": "client-two",
        "access_token": "access-two",
    }
    auth = ChatGptAuth(store)
    monkeypatch.setattr(
        engine_module,
        "cloud_json",
        lambda *a, **kw: {
            "models": [{"slug": "gpt-6-luna", "display_name": "Luna", "visibility": "list"}]
        },
    )
    provider = ChatGptProvider(
        CheckSettings(llm_provider="chatgpt", llm_chatgpt_allow_cloud=True), auth
    )
    assert provider.probe().status == "available"
    auth.select("two")
    requests = install_stream(
        monkeypatch,
        sse(
            {"type": "response.output_text.delta", "delta": '{"findings": []}'},
            {
                "type": "response.completed",
                "response": {"model": "gpt-6-luna", "status": "completed"},
            },
        ),
    )
    provider.complete("s", "u", {})
    assert requests[0].get_header("Authorization") == "Bearer synthetic-access"
    store.data["accounts"]["one"]["state"] = "signed_out"
    with pytest.raises(CloudError, match="oauth_sign_in_required"):
        provider.complete("s", "u", {})
    assert len(requests) == 1


def test_adaptive_conflict_with_local_engine_is_routed(
    tmp_path: Path, isolated_appdata: Path, json_server: Any
) -> None:
    source = tmp_path / "input.md"
    source.write_text("Эта ошыбка.\n", encoding="utf-8")
    json_server.responder = languagetool_responder({"ошыбка": ["ошибочка"]})
    config = replace(
        load_config(),
        check=CheckSettings(
            engines=("languagetool", "llm"),
            languagetool_endpoint=json_server.url,
            analysis_mode="adaptive",
            analysis_strategies=("grammar",),
        ),
    )
    provider = Provider(lambda s, p: {"findings": [finding(p["items"][0]["id"])]})
    result = check_paths(
        [str(source)],
        config,
        engine_factory=lambda n, s: LlmEngine(s, provider) if n == "llm" else LanguageToolEngine(s),
    )
    assert len(provider.calls) == 2
    extra = result.engines[-1].details["analysis"]["passes"][1]
    assert extra["units"][0]["routing"] == ["conflicting_engine_evidence"]
    assert {s.engine_replacement for f in result.documents[0].findings for s in f.suggestions} == {
        "ошибка",
        "ошибочка",
    }
