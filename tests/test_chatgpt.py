"""Контракт SIWC: реальные JWT/DPAPI, контролируемый HTTP; это не live OAuth."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from language_review import chatgpt_auth as auth_module
from language_review import chatgpt_http as http
from language_review.chatgpt_auth import ChatGptAuth, LoginAttempt, token_record, validate_identity
from language_review.chatgpt_http import CloudError
from language_review.chatgpt_store import CredentialStore, dpapi
from language_review.config import CheckSettings
from language_review.engine_chatgpt import ChatGptProvider


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> Any:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test-key"}
    monkeypatch.setattr(auth_module, "discovery", lambda: {"jwks_uri": http.ISSUER + "/keys"})
    monkeypatch.setattr(auth_module, "cloud_json", lambda *a, **kw: {"keys": [jwk]})

    def sign(**claims: Any) -> str:
        data = {
            "sub": "synthetic-subject",
            "iss": http.ISSUER,
            "aud": "test-client",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "nonce": "test-nonce",
        }
        return jwt.encode(data | claims, key, algorithm="RS256", headers={"kid": "test-key"})

    return sign


def test_oidc_validates_signature_and_identity(identity: Any) -> None:
    assert validate_identity(identity(), "test-client", "test-nonce")["sub"] == "synthetic-subject"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://invalid.example"},
        {"aud": "other-client"},
        {"exp": 1},
        {"nonce": "other-nonce"},
        {"sub": ""},
        {"iat": int(time.time()) + 3600},
        {"aud": ["test-client", "other"], "azp": "other"},
    ],
)
def test_oidc_rejects_wrong_claims(identity: Any, claims: dict[str, Any]) -> None:
    with pytest.raises(CloudError):
        validate_identity(identity(**claims), "test-client", "test-nonce")


def test_oidc_rejects_invalid_signature_and_unknown_key(identity: Any) -> None:
    token = identity()
    parts = token.split(".")
    parts[-1] = base64.urlsafe_b64encode(b"wrong-signature").decode().rstrip("=")
    with pytest.raises(CloudError, match="oidc_invalid_token"):
        validate_identity(".".join(parts), "test-client", "test-nonce")
    parts = token.split(".")
    parts[0] = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "unknown"}).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(CloudError, match="oidc_unknown_key"):
        validate_identity(".".join(parts), "test-client", "test-nonce")


def test_dynamic_registration_uses_issued_client_pkce_and_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = LoginAttempt("urn:uuid:synthetic-host", "http://127.0.0.1:1234/auth/callback")
    query = parse_qs(urlsplit(attempt.url()).query)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["agent_name_hint"] == ["Language Review"]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    captured = []
    monkeypatch.setattr(
        auth_module, "cloud_json", lambda url, **kw: captured.append((url, kw)) or {"tokens": True}
    )
    client, tokens = attempt.exchange(
        urlencode({"state": attempt.state, "code": "test-code", "client_id": "issued-client"})
    )
    assert client == "issued-client" and tokens == {"tokens": True}
    assert captured == [
        (
            http.TOKEN,
            {
                "form": {
                    "grant_type": "authorization_code",
                    "client_id": "issued-client",
                    "code": "test-code",
                    "code_verifier": attempt.verifier,
                    "redirect_uri": attempt.redirect_uri,
                    "resource": http.API,
                }
            },
        )
    ]
    with pytest.raises(CloudError, match="oauth_attempt_expired"):
        attempt.exchange("")


@pytest.mark.parametrize(
    "query,code",
    [
        ({"state": "wrong", "code": "x", "client_id": "issued"}, "oauth_state_mismatch"),
        ({"error": "access_denied"}, "oauth_access_denied"),
        ({"code": "x"}, "oauth_client_mismatch"),
        ({"client_id": "issued"}, "oauth_code_missing"),
    ],
)
def test_callback_errors_never_exchange_tokens(
    monkeypatch: pytest.MonkeyPatch, query: dict[str, str], code: str
) -> None:
    attempt = LoginAttempt("host", "http://127.0.0.1:1234/auth/callback")

    def forbidden(*args: Any, **kw: Any) -> None:
        pytest.fail("invalid callback must not exchange a code")

    monkeypatch.setattr(auth_module, "cloud_json", forbidden)
    with pytest.raises(CloudError, match=code):
        attempt.exchange(urlencode({"state": attempt.state} | query))


def test_returning_registration_reuses_client_and_rejects_client_switch() -> None:
    attempt = LoginAttempt("host", "http://127.0.0.1:1234/auth/callback", client="saved-client")
    query = parse_qs(urlsplit(attempt.url({"id_token": "synthetic-hint"})).query)
    assert query["client_id"] == ["saved-client"] and "agent_name_hint" not in query
    assert query["id_token_hint"] == ["synthetic-hint"]
    with pytest.raises(CloudError, match="oauth_client_mismatch"):
        attempt.exchange(urlencode({"state": attempt.state, "client_id": "other", "code": "x"}))


def test_token_scopes_identity_and_rotation(identity: Any) -> None:
    tokens = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "id_token": identity(),
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "openid resource.invoke",
    }
    row = token_record(tokens, "test-client", nonce="test-nonce")
    assert "chatgpt.tokens.use.direct" not in row["scopes"]  # signed in, plan disabled
    updated = token_record(
        tokens | {"refresh_token": "rotated", "id_token": identity()}, "test-client", previous=row
    )
    assert updated["refresh_token"] == "rotated"
    with pytest.raises(CloudError, match="oauth_account_mismatch"):
        token_record(tokens | {"id_token": identity(sub="other")}, "test-client", previous=row)
    with pytest.raises(CloudError, match="oauth_scopes_missing"):
        token_record(tokens | {"scope": "resource.invoke"}, "test-client", nonce="test-nonce")


def test_identity_only_sign_in_is_retained_without_plan_permission(identity: Any) -> None:
    record = token_record(
        {"id_token": identity(), "token_type": "Bearer", "scope": "openid profile email"},
        "test-client",
        nonce="test-nonce",
    )
    store = MemoryStore()
    store.data["accounts"]["one"] = record
    auth = ChatGptAuth(store)
    assert auth.status()["accounts"][0]["plan_enabled"] is False
    with pytest.raises(CloudError, match="oauth_plan_permission_missing"):
        auth.access_token()


def test_reauthorization_requires_fresh_id_token_even_for_saved_account(identity: Any) -> None:
    row = token_record(
        {"id_token": identity(), "token_type": "Bearer", "scope": "openid"},
        "test-client",
        nonce="test-nonce",
    )
    tokens = {
        "access_token": "new",
        "refresh_token": "new-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": auth_module.SCOPES,
    }
    with pytest.raises(CloudError, match="oauth_id_token_missing"):
        token_record(tokens, "test-client", previous=row, nonce="fresh-nonce")


class MemoryStore:
    """Тестовый storage boundary, не production fallback."""

    def __init__(self) -> None:
        self.data = {
            "version": 1,
            "host_id": "urn:uuid:synthetic",
            "active": "one",
            "accounts": {
                "one": {
                    "client_id": "test-client",
                    "subject": "synthetic-subject",
                    "state": "signed_in",
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "id_token": "synthetic-id",
                    "expires_at": time.time() + 1000,
                    "scopes": auth_module.SCOPES.split(),
                }
            },
        }

    def locked(self) -> Any:
        from contextlib import nullcontext

        return nullcontext(self.data)

    def save(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)


def test_refresh_terminal_error_clears_only_tokens_and_retains_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    store.data["accounts"]["two"] = {"client_id": "other-client", "state": "signed_out"}

    def revoked(*args: Any, **kw: Any) -> None:
        raise CloudError("invalid_grant")

    monkeypatch.setattr(auth_module, "cloud_json", revoked)
    with pytest.raises(CloudError, match="invalid_grant"):
        ChatGptAuth(store).access_token(force=True)
    row = store.data["accounts"]["one"]
    assert row["client_id"] == "test-client" and row["state"] == "signed_out"
    assert not {"access_token", "refresh_token", "id_token"} & row.keys()
    assert store.data["accounts"]["two"] == {"client_id": "other-client", "state": "signed_out"}


def test_refresh_network_failure_preserves_session(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    before = copy.deepcopy(store.data)

    def failed(*args: Any, **kw: Any) -> None:
        raise CloudError("connection_failed")

    monkeypatch.setattr(auth_module, "cloud_json", failed)
    with pytest.raises(CloudError, match="connection_failed"):
        ChatGptAuth(store).access_token(force=True)
    assert store.data == before


@pytest.mark.parametrize("confirmed", [True, False])
def test_logout_clears_tokens_and_reports_remote_revocation(
    monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    store = MemoryStore()
    calls = []
    monkeypatch.setattr(
        auth_module, "discovery", lambda: {"revocation_endpoint": http.ISSUER + "/revoke"}
    )
    monkeypatch.setattr(auth_module.time, "sleep", lambda _: None)

    def revoke(url: str, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        if not confirmed:
            raise CloudError("http_503", status=503)
        return {}

    monkeypatch.setattr(auth_module, "cloud_json", revoke)
    assert ChatGptAuth(store).logout() is confirmed
    assert calls[0]["form"] == {
        "token": "synthetic-refresh",
        "token_type_hint": "refresh_token",
        "client_id": "test-client",
    }
    assert len(calls) == (1 if confirmed else 3)
    assert store.data["active"] is None
    assert store.data["accounts"]["one"]["state"] == "signed_out"
    assert "synthetic-access" not in json.dumps(store.data)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI acceptance")
def test_windows_dpapi_storage_and_concurrent_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(tmp_path / "credentials")
    with store.locked() as database:
        database.update(MemoryStore().data)
        database["accounts"]["one"]["expires_at"] = 1
        store.save(database)
    assert b"synthetic-refresh" not in store.path.read_bytes()
    assert dpapi(dpapi(b"synthetic-secret"), decrypt=True) == b"synthetic-secret"
    calls = []

    def refresh(url: str, **kw: Any) -> dict[str, Any]:
        calls.append(kw["form"])
        time.sleep(0.1)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    monkeypatch.setattr(auth_module, "cloud_json", refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ChatGptAuth(store).access_token(), range(2)))
    assert results == ["new-access", "new-access"] and len(calls) == 1
    with store.locked() as database:
        assert database["accounts"]["one"]["refresh_token"] == "new-refresh"
        assert database["host_id"] == "urn:uuid:synthetic"


@pytest.mark.skipif(os.name != "nt", reason="Windows cross-process DPAPI lock")
def test_windows_refresh_is_serialized_across_processes(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials")
    with store.locked() as database:
        database.update(MemoryStore().data)
        database["accounts"]["one"]["expires_at"] = 1
        store.save(database)
    script = tmp_path / "refresh.py"
    script.write_text(
        """from pathlib import Path
import sys, time
from language_review import chatgpt_auth
from language_review.chatgpt_store import CredentialStore
root = Path(sys.argv[1])
def refresh(*args, **kwargs):
    with (root / "refresh-count").open("a") as output:
        output.write("request\\n")
    time.sleep(0.3)
    return {"access_token": "rotated-access", "refresh_token": "rotated-refresh",
            "token_type": "Bearer", "expires_in": 3600}
chatgpt_auth.cloud_json = refresh
assert chatgpt_auth.ChatGptAuth(CredentialStore(root)).access_token() == "rotated-access"
print("ok")
""",
        encoding="utf-8",
    )
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(store.directory)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(2)
    ]
    for process in processes:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0 and out.strip() == "ok" and not err
    assert (store.directory / "refresh-count").read_text().splitlines() == ["request"]


@pytest.mark.parametrize("denied,invalid_grant", [(False, False), (True, False), (False, True)])
def test_loopback_login_and_new_code_recovery(
    identity: Any, monkeypatch: pytest.MonkeyPatch, denied: bool, invalid_grant: bool
) -> None:
    store = MemoryStore()
    original = copy.deepcopy(store.data)
    browser_queries = []
    callbacks = []
    exchanges = []
    key_reader = auth_module.cloud_json

    def open_browser(url: str) -> bool:
        parameters = parse_qs(urlsplit(url).query)
        browser_queries.append(parameters)
        query = {"state": parameters["state"][0]}
        query.update(
            {"error": "access_denied"}
            if denied
            else {"client_id": "new-client", "code": "code-" + str(len(browser_queries))}
        )

        def callback() -> None:
            with urllib.request.urlopen(
                parameters["redirect_uri"][0] + "?" + urlencode(query), timeout=5
            ) as response:
                assert response.status == 200 and response.headers["Cache-Control"] == "no-store"

        thread = threading.Thread(target=callback)
        callbacks.append(thread)
        thread.start()
        return True

    def exchange(url: str, **kw: Any) -> Any:
        if url != http.TOKEN:
            return key_reader(url, **kw)
        exchanges.append(kw["form"])
        if invalid_grant and len(exchanges) == 1:
            raise CloudError("invalid_grant")
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": auth_module.SCOPES,
            "id_token": identity(aud="new-client", nonce=browser_queries[-1]["nonce"][0]),
        }

    monkeypatch.setattr(auth_module.webbrowser, "open", open_browser)
    monkeypatch.setattr(auth_module, "cloud_json", exchange)
    try:
        if denied:
            with pytest.raises(CloudError, match="oauth_access_denied"):
                ChatGptAuth(store).login(timeout=3)
            assert store.data == original and not exchanges
        else:
            selected = ChatGptAuth(store).login(timeout=3)
            assert selected != "one" and store.data["active"] == selected
            assert store.data["accounts"]["one"] == original["accounts"]["one"]
            assert store.data["accounts"][selected]["client_id"] == "new-client"
            assert browser_queries[0]["client_id"] == ["dynamic_agent_client"]
            if invalid_grant:
                assert browser_queries[1]["client_id"] == ["new-client"]
                assert "agent_name_hint" not in browser_queries[1]
                assert browser_queries[0]["state"] != browser_queries[1]["state"]
                assert exchanges[0]["code_verifier"] != exchanges[1]["code_verifier"]
    finally:
        for thread in callbacks:
            thread.join(timeout=5)


def test_cancelled_and_timed_out_login_preserve_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    original = copy.deepcopy(store.data)
    monkeypatch.setattr(auth_module.webbrowser, "open", lambda _: True)
    with pytest.raises(CloudError, match="oauth_login_timeout"):
        ChatGptAuth(store).login(timeout=0.01)
    assert store.data == original

    def cancel(_: str) -> bool:
        raise KeyboardInterrupt()

    monkeypatch.setattr(auth_module.webbrowser, "open", cancel)
    with pytest.raises(KeyboardInterrupt):
        ChatGptAuth(store).login()
    assert store.data == original


class Stream(io.BytesIO):
    def __init__(self, data: bytes, content_type: str = "text/event-stream") -> None:
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


def install_stream(monkeypatch: pytest.MonkeyPatch, data: bytes) -> list[Any]:
    requests = []

    class Opener:
        def open(self, request: Any, **kw: Any) -> Stream:
            requests.append(request)
            return Stream(data)

    monkeypatch.setattr(http, "_OPENER", Opener())
    return requests


def test_responses_public_route_allowed_payload_and_completed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = sse(
        {"type": "response.output_text.delta", "delta": '{"findings": []}'},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "model": "gpt-6-luna",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        },
    )
    requests = install_stream(monkeypatch, data)
    auth = ChatGptAuth(MemoryStore())
    provider = ChatGptProvider(
        CheckSettings(llm_provider="chatgpt", llm_chatgpt_allow_cloud=True), auth
    )
    provider.label = "one"
    reply = provider.complete("instructions", "masked text", {"type": "object"})
    assert reply.content == '{"findings": []}' and reply.prompt_tokens == 10
    [request] = requests
    assert request.full_url == "https://api.openai.com/v1/responses"
    payload = json.loads(request.data)
    assert set(payload) == {"model", "store", "stream", "input", "instructions", "reasoning"}
    assert payload["store"] is False and payload["stream"] is True
    assert payload["input"] == [{"role": "user", "content": "masked text"}]
    assert payload["reasoning"] == {"effort": "medium"}
    assert request.get_header("Authorization") == "Bearer synthetic-access"


@pytest.mark.parametrize(
    "events,code",
    [
        (
            [{"type": "response.output_text.delta", "delta": '{"findings": []}'}],
            "sse_missing_completed",
        ),
        ([{"type": "response.incomplete"}], "response_incomplete"),
        (
            [
                {
                    "type": "response.failed",
                    "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
                }
            ],
            "subscription_sharing_usage_limit_exceeded",
        ),
        (
            [
                {
                    "type": "error",
                    "error": {
                        "code": "subscription_sharing_unsupported_capability",
                        "param": "reasoning.effort",
                    },
                }
            ],
            "subscription_sharing_unsupported_capability",
        ),
        (
            [{"type": "response.completed", "response": {"status": "incomplete"}}],
            "sse_protocol_error",
        ),
    ],
)
def test_sse_failures_never_return_partial_text_as_success(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]], code: str
) -> None:
    install_stream(monkeypatch, sse(*events))
    with pytest.raises(CloudError, match=code):
        http.stream_response({"model": "gpt-6-luna"}, "synthetic-access", 1)


@pytest.mark.parametrize(
    "raw,code", [(b"data: {oops}\n\n", "sse_invalid_json"), (b"data: \xff\n\n", "sse_invalid_utf8")]
)
def test_invalid_sse(monkeypatch: pytest.MonkeyPatch, raw: bytes, code: str) -> None:
    install_stream(monkeypatch, raw)
    with pytest.raises(CloudError, match=code):
        http.stream_response({"model": "gpt-6-luna"}, "synthetic", 1)


def test_cloud_consent_and_model_catalog_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import language_review.engine_chatgpt as engine_module

    auth = ChatGptAuth(MemoryStore())
    provider = ChatGptProvider(CheckSettings(llm_provider="chatgpt"), auth)
    assert provider.probe().diagnostics == ["cloud_consent_required"]
    with pytest.raises(CloudError, match="cloud_consent_required"):
        provider.complete("s", "u", {})
    calls = []

    def catalog(url: str, **kw: Any) -> Any:
        calls.append(url)
        return {"models": [{"slug": "gpt-6.1-sol", "display_name": "Sol", "visibility": "list"}]}

    monkeypatch.setattr(engine_module, "cloud_json", catalog)
    provider = ChatGptProvider(
        CheckSettings(llm_provider="chatgpt", llm_chatgpt_allow_cloud=True), auth
    )
    assert provider.probe().diagnostics == ["selected_model_unavailable"]
    assert calls == [http.API + "/models"]
    assert provider.model == "gpt-6-luna"


def test_http_errors_redact_detail_and_preserve_code_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, request: Any, **kw: Any) -> None:
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "forbidden",
                Message(),
                io.BytesIO(b'{"detail":"synthetic-access confidential text"}'),
            )

    monkeypatch.setattr(http, "_OPENER", Opener())
    with pytest.raises(CloudError) as caught:
        http.cloud_json(http.API + "/models", token="synthetic-access")
    assert caught.value.metadata()["body_shape"] == "detail"
    assert caught.value.http_status == 403 and str(caught.value) == "http_403"
    assert "synthetic-access" not in json.dumps(caught.value.metadata())


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1/models",
        "https://api.openai.com@invalid.example/",
        "https://auth.openai.com:444/keys",
        "https://invalid.example/",
    ],
)
def test_cloud_endpoint_is_fixed(url: str) -> None:
    with pytest.raises(CloudError, match="unsafe_cloud_endpoint"):
        http.cloud_json(url)


@pytest.mark.parametrize(
    "content,terminal,expected",
    [
        ('{"findings": []}', "completed", "clean"),
        ('{"findings": [null]}', "completed", "unavailable"),
        ('{"findings":', "completed", "unavailable"),
        ('{"findings": []}', "incomplete", "unavailable"),
        ('{"findings": []}', "missing", "unavailable"),
    ],
)
def test_cloud_product_check_validates_stream_and_same_review_contract(
    tmp_path: Path,
    isolated_appdata: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    terminal: str,
    expected: str,
) -> None:
    from dataclasses import replace

    import language_review.engine_chatgpt as engine_module
    from language_review.check import check_paths
    from language_review.config import load_config
    from language_review.engine_llm import LlmEngine
    from language_review.review_report import review_document
    from language_review.review_verify import load_review, verify_review

    source = tmp_path / "input.rs"
    original = b"// " + "Проверьте".encode() + b" my_token {0}.\n"
    source.write_bytes(original)
    terminal_event = (
        {"type": "response.completed", "response": {"model": "gpt-6-luna", "status": "completed"}}
        if terminal == "completed"
        else {"type": "response.incomplete"}
    )
    events = [{"type": "response.output_text.delta", "delta": content}]
    if terminal != "missing":
        events.append(terminal_event)
    requests = install_stream(monkeypatch, sse(*events))
    monkeypatch.setattr(
        engine_module,
        "cloud_json",
        lambda *a, **kw: {
            "models": [{"slug": "gpt-6-luna", "display_name": "Luna", "visibility": "list"}]
        },
    )
    config = replace(
        load_config(),
        check=CheckSettings(engines=("llm",), llm_provider="chatgpt", llm_chatgpt_allow_cloud=True),
    )
    result = check_paths(
        [str(source)],
        config,
        engine_factory=lambda n, settings: LlmEngine(
            settings, ChatGptProvider(settings, ChatGptAuth(MemoryStore()))
        ),
    )
    assert result.status == expected and source.read_bytes() == original
    assert len(requests) == 1
    sent = json.loads(requests[0].data)
    assert (
        "my_token" not in sent["input"][0]["content"] and "{0}" not in sent["input"][0]["content"]
    )
    report = review_document(result)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert load_review(report_path)["status"] == expected
    assert verify_review(report_path, {"root-0001": source})["status"] == "current"


def test_cli_cloud_consent_config_and_doctor_are_observable(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.check_helpers import _run

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    source = tmp_path / "input.md"
    source.write_text("Верный текст.\n", encoding="utf-8")
    config = tmp_path / "cloud.toml"
    config.write_text(
        '[check]\nengines = ["llm"]\n[check.llm]\nprovider = "chatgpt"\n'
        '[check.analysis]\nmode = "deep"\n',
        encoding="utf-8",
    )
    process = _run(
        ["--config", str(config), "check", str(source), "--view", "review", "--format", "json"],
        isolated_appdata,
    )
    assert process.returncode == 5 and "Traceback" not in process.stderr
    report = json.loads(process.stdout)
    assert report["status"] == "unavailable"
    assert report["analysis"]["engines"][0]["diagnostics"] == [
        "cloud_consent_required",
        "engine_unavailable",
    ]
    assert report["analysis"]["engines"][0]["details"]["analysis_policy"]["mode"] == "deep"
    process = _run(["--config", str(config), "doctor", "--format", "json"], isolated_appdata)
    doctor = json.loads(process.stdout)
    assert "cloud_consent_required" in json.dumps(doctor)
    process = _run(["chatgpt", "status"], isolated_appdata)
    assert process.returncode == 0 and json.loads(process.stdout) == {
        "active": None,
        "accounts": [],
    }
