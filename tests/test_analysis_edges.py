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
from language_review.masking import EngineView


def _verify_local_hypothesis(
    tmp_path: Path,
    json_server: Any,
    text: str,
    misspellings: dict[str, list[str]],
    *,
    verification_reply: str | None = None,
    **options: Any,
) -> tuple[Any, Provider]:
    source = tmp_path / "input.txt"
    source.write_text(text, encoding="utf-8")
    json_server.responder = languagetool_responder(misspellings)
    config = replace(
        load_config(),
        check=CheckSettings(
            engines=("languagetool", "llm"),
            languagetool_endpoint=json_server.url,
            analysis_verify=True,
            **options,
        ),
    )

    def respond(_system: str, payload: dict[str, Any]) -> Any:
        if "hypotheses" not in payload:
            return {"findings": []}
        if verification_reply is not None:
            return verification_reply
        [hypothesis] = payload["hypotheses"]
        return {
            "decisions": [
                {
                    "id": hypothesis["id"],
                    "excerpt": hypothesis["excerpt"],
                    "decision": "confirmed",
                    "evidence": "Позиция сверена по синтетическому контексту.",
                }
            ]
        }

    provider = Provider(respond)
    before = source.read_bytes()
    result = check_paths(
        [str(source)],
        config,
        engine_factory=lambda name, settings: (
            LlmEngine(settings, provider) if name == "llm" else LanguageToolEngine(settings)
        ),
    )
    assert source.read_bytes() == before
    return result, provider


@pytest.mark.parametrize("excess", [0, 1])
def test_local_hypothesis_verification_obeys_context_boundary(
    tmp_path: Path, isolated_appdata: Path, json_server: Any, excess: int
) -> None:
    terms = {"термин": "предпочтительное слово"}
    limit = len(json.dumps(terms, ensure_ascii=False)) - excess
    result, provider = _verify_local_hypothesis(
        tmp_path,
        json_server,
        "Эта ошыбка.",
        {"ошыбка": ["ошибка"]},
        terms=terms,
        analysis_max_context_chars=limit,
    )
    llm = next(e for e in result.engines if e.name == "llm")
    verification = llm.details["analysis"]["passes"][1]
    [found] = result.documents[0].findings
    assert found.suggestions[0].engine_replacement == "ошибка"
    assert {c.raw.engine for c in found.candidates} == {"languagetool"}
    if excess:
        assert provider.calls == []
        assert result.status in {"partial", "unavailable"}
        assert verification["status"] == "failed"
        assert verification["reason"] == "analysis_context_budget"
        assert "analysis_context_budget" in llm.diagnostics
        assert verification["decisions"][0]["decision"] == "uncertain"
        assert verification["decisions"][0]["hypothesis"]["replacements"] == ["ошибка"]
    else:
        assert len(provider.calls) == 2
        assert result.status == "findings"
        assert verification["status"] == "checked"
        assert verification["decisions"][0]["decision"] == "confirmed"


@pytest.mark.parametrize(
    "reply",
    ["[" * 100_000 + "0" + "]" * 100_000, '{"decisions": []}', '{"decisions": ['],
    ids=["deep-json", "invalid-schema", "invalid-json"],
)
def test_invalid_verification_response_preserves_local_evidence(
    tmp_path: Path, isolated_appdata: Path, json_server: Any, reply: str
) -> None:
    result, provider = _verify_local_hypothesis(
        tmp_path,
        json_server,
        "Эта ошыбка.\n\nЭтот текст верен.",
        {"ошыбка": ["ошибка"]},
        verification_reply=reply,
    )
    llm = next(e for e in result.engines if e.name == "llm")
    verification = llm.details["analysis"]["passes"][1]
    assert result.status == "partial" and len(provider.calls) == 2
    assert llm.request_failures == 1
    assert verification["status"] == "failed"
    assert verification["reason"] == "verification_invalid_response"
    [decision] = verification["decisions"]
    assert decision["decision"] == "uncertain"
    assert decision["hypothesis"]["excerpt"] == "ошыбка"
    assert decision["hypothesis"]["engine"] == "languagetool"
    [found] = result.documents[0].findings
    assert found.suggestions[0].engine_replacement == "ошибка"
    assert len(found.candidates) == 1


def test_recursive_verification_validation_is_diagnostic(
    tmp_path: Path, isolated_appdata: Path, json_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def validate(*args: Any) -> None:
        raise RecursionError("Синтетическое превышение глубины валидации ответа.")

    monkeypatch.setattr(
        "language_review.analysis.jsonschema.Draft202012Validator.validate", validate
    )
    result, _provider = _verify_local_hypothesis(
        tmp_path, json_server, "Эта ошыбка.\n\nЭтот текст верен.", {"ошыбка": ["ошибка"]}
    )
    llm = next(e for e in result.engines if e.name == "llm")
    assert result.status == "partial" and llm.request_failures == 1
    verification = llm.details["analysis"]["passes"][1]
    assert verification["reason"] == "verification_invalid_response"
    assert verification["decisions"][0]["decision"] == "uncertain"
    assert result.documents[0].findings[0].suggestions[0].engine_replacement == "ошибка"


@pytest.mark.parametrize("allow_memory", [False, True])
def test_verifier_context_preserves_terms_and_consented_memory_at_boundary(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    allow_memory: bool,
) -> None:
    terms = {"термин": "предпочтительное слово"}
    memory = [{"id": "rule", "text": "Проверяйте точную цитату.", "state": "rejected"}]
    limit = len(json.dumps(terms, ensure_ascii=False)) + len(json.dumps(memory, ensure_ascii=False))

    class Session:
        def prepare(self, units: Any) -> None:
            for unit in units:
                unit.memory_context = memory

        def finish(self, result: Any) -> None:
            pass

    monkeypatch.setattr(
        "language_review.memory.pipeline.MemorySession.open", lambda *a, **kw: Session()
    )
    result, provider = _verify_local_hypothesis(
        tmp_path,
        json_server,
        "Эта ошыбка.",
        {"ошыбка": ["ошибка"]},
        terms=terms,
        analysis_max_context_chars=limit,
        llm_provider="chatgpt",
        llm_chatgpt_allow_cloud=True,
        llm_chatgpt_allow_memory=allow_memory,
    )
    llm = next(e for e in result.engines if e.name == "llm")
    assert result.status == "findings"
    assert llm.details["analysis"]["memory_withheld"] == (0 if allow_memory else 1)
    assert len(provider.calls) == 2
    verification_input = provider.calls[1][1]
    assert verification_input["terms"] == terms
    assert verification_input["memory_context"] == (memory if allow_memory else [])
    assert llm.details["analysis"]["passes"][1]["status"] == "checked"


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


@pytest.mark.parametrize(
    ("text", "misspellings", "expected_excerpt"),
    [
        ("в доме и в поле.", {"в": ["во"]}, "в"),
        (
            "Используйте FooBar теперь.",
            {"Используйте X теперь": ["Используйте FooBar правильно"]},
            "Используйте ⟦1⟧ теперь",
        ),
    ],
    ids=["repeated-quote", "protected-token"],
)
def test_verifier_uses_local_source_range_in_llm_view(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: Any,
    text: str,
    misspellings: dict[str, list[str]],
    expected_excerpt: str,
) -> None:
    result, provider = _verify_local_hypothesis(tmp_path, json_server, text, misspellings)

    llm_run = next(engine for engine in result.engines if engine.name == "llm")
    verification = llm_run.details["analysis"]["passes"][1]
    assert result.status == "findings"
    assert verification["status"] == "checked"
    assert verification["decisions"][0]["decision"] == "confirmed"
    assert verification["decisions"][0]["hypothesis"]["excerpt"] == expected_excerpt
    assert provider.calls[1][1]["hypotheses"][0]["excerpt"] == expected_excerpt


def test_unmappable_local_hypothesis_does_not_fail_check_unit(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(EngineView, "view_range", lambda self, start, end: None)

    result, _provider = _verify_local_hypothesis(
        tmp_path, json_server, "в доме и в поле.", {"в": ["во"]}
    )

    llm_run = next(engine for engine in result.engines if engine.name == "llm")
    verification = llm_run.details["analysis"]["passes"][1]
    assert result.status == "findings"
    assert verification["status"] == "not_applicable"
    assert verification["reason"] == "verification_unlocalized"
    assert "verification_unlocalized" in llm_run.diagnostics


def test_unmappable_hypothesis_does_not_hide_prior_verification_failure(
    tmp_path: Path,
    isolated_appdata: Path,
    json_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Это опечатка и хыбака."
    source = tmp_path / "input.txt"
    source.write_text(text, encoding="utf-8")
    json_server.responder = languagetool_responder({"опечатка": ["опечатку"], "хыбака": ["рыбака"]})
    original_view_range = EngineView.view_range

    def view_range(self: EngineView, start: int, end: int) -> tuple[int, int] | None:
        if self.text[start:end] == "хыбака":
            return None
        return original_view_range(self, start, end)

    monkeypatch.setattr(EngineView, "view_range", view_range)
    config = replace(
        load_config(),
        check=CheckSettings(
            engines=("languagetool", "llm"),
            languagetool_endpoint=json_server.url,
            analysis_verify=True,
        ),
    )

    def respond(_system: str, payload: dict[str, Any]) -> Any:
        if "hypotheses" not in payload:
            return {"findings": []}
        hypothesis = payload["hypotheses"][0]
        return {
            "decisions": [
                {
                    "id": hypothesis["id"],
                    "excerpt": "неверная цитата",
                    "decision": "confirmed",
                    "evidence": "Проверочная цитата намеренно не совпадает.",
                }
            ]
        }

    provider = Provider(respond)
    result = check_paths(
        [str(source)],
        config,
        engine_factory=lambda name, settings: (
            LlmEngine(settings, provider) if name == "llm" else LanguageToolEngine(settings)
        ),
    )

    llm_run = next(engine for engine in result.engines if engine.name == "llm")
    verification = llm_run.details["analysis"]["passes"][1]
    assert result.documents[0].status == "partial"
    assert verification["status"] == "failed"
    assert verification["reason"] == "verification_invalid_response"
    assert [decision["decision"] for decision in verification["decisions"]] == [
        "uncertain",
        "uncertain",
    ]
