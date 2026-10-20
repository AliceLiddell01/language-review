"""Собственная SIWC OAuth-регистрация; credentials Codex никогда не читаются."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from language_review.chatgpt_http import API, DISCOVERY, ISSUER, TOKEN, CloudError, cloud_json
from language_review.chatgpt_store import CredentialStore

SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
TERMINAL_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


def discovery() -> dict[str, Any]:
    result = cloud_json(DISCOVERY)
    if not isinstance(result, dict) or result.get("issuer") != ISSUER:
        raise CloudError("oidc_discovery_invalid")
    for key in ("jwks_uri", "revocation_endpoint"):
        value = result.get(key)
        if value is not None and (not isinstance(value, str) or not value.startswith(ISSUER + "/")):
            raise CloudError("oidc_discovery_invalid")
    return result


def validate_identity(token: str, client: str, nonce: str | None) -> dict[str, Any]:
    try:
        import jwt
    except ImportError:
        raise CloudError("chatgpt_extra_required") from None
    try:
        metadata = discovery()
        keys = cloud_json(metadata["jwks_uri"])
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise CloudError("oidc_invalid_token")
        matches = [key for key in keys["keys"] if key.get("kid") == header.get("kid")]
        if len(matches) != 1:
            raise CloudError("oidc_unknown_key")
        identity = jwt.decode(
            token,
            jwt.PyJWK.from_dict(matches[0]).key,
            algorithms=["RS256"],
            audience=client,
            issuer=ISSUER,
            leeway=5,
            options={"require": ["sub", "exp", "iat", "aud", "iss"]},
        )
        if not isinstance(identity.get("sub"), str) or not identity["sub"]:
            raise CloudError("oidc_invalid_identity")
        if nonce is not None and not hmac.compare_digest(str(identity.get("nonce", "")), nonce):
            raise CloudError("oidc_nonce_mismatch")
        if (
            isinstance(identity["aud"], list)
            and len(identity["aud"]) > 1
            and identity.get("azp") != client
        ):
            raise CloudError("oidc_invalid_audience")
        return dict(identity)
    except KeyError, TypeError, ValueError, AttributeError, jwt.PyJWTError:
        raise CloudError("oidc_invalid_token") from None


@dataclass
class LoginAttempt:
    host_id: str
    redirect_uri: str
    client: str = "dynamic_agent_client"
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(64))
    deadline: float = field(default_factory=lambda: time.monotonic() + 300)
    consumed: bool = False

    def url(self, previous: dict[str, Any] | None = None, consent: bool = False) -> str:
        parameters = {
            "client_id": self.client,
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "resource": API,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
        }
        if self.client == "dynamic_agent_client":
            parameters["agent_name_hint"] = "Language Review"
        elif previous and previous.get("id_token"):
            parameters["id_token_hint"] = previous["id_token"]
        if consent:
            parameters["prompt"] = "consent"
        return ISSUER + "/api/accounts/authorize?" + urlencode(parameters)

    def exchange(self, query: str) -> tuple[str, dict[str, Any]]:
        if self.consumed or time.monotonic() >= self.deadline:
            raise CloudError("oauth_attempt_expired")
        self.consumed = True
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            raise CloudError("oauth_callback_invalid") from None
        if any(len(value) != 1 for value in values.values()) or not hmac.compare_digest(
            values.get("state", [""])[0].encode(), self.state.encode()
        ):
            raise CloudError("oauth_state_mismatch")
        if "error" in values:
            raise CloudError("oauth_access_denied")
        client = values.get("client_id", [self.client])[0]
        if (
            not client
            or client == "dynamic_agent_client"
            or (self.client != "dynamic_agent_client" and client != self.client)
        ):
            raise CloudError("oauth_client_mismatch")
        self.client = client  # retain issued registration even if exchange needs a fresh code
        code = values.get("code", [""])[0]
        if not code:
            raise CloudError("oauth_code_missing")
        tokens = cloud_json(
            TOKEN,
            form={
                "grant_type": "authorization_code",
                "client_id": client,
                "code": code,
                "code_verifier": self.verifier,
                "redirect_uri": self.redirect_uri,
                "resource": API,
            },
        )
        return client, tokens


def token_record(
    tokens: Any, client: str, *, previous: dict[str, Any] | None = None, nonce: str | None = None
) -> dict[str, Any]:
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get("token_type"), str)
        or tokens["token_type"].lower() != "bearer"
    ):
        raise CloudError("oauth_invalid_token_response")
    expires = tokens.get("expires_in")
    if expires is not None and (type(expires) is not int or not 0 < expires <= 86400):
        raise CloudError("oauth_invalid_token_response")
    id_token = tokens.get("id_token")
    if id_token is None and previous is not None and nonce is None:
        identity = {"sub": previous["subject"]}
        id_token = previous.get("id_token")
    elif isinstance(id_token, str) and id_token:
        identity = validate_identity(id_token, client, nonce)
    else:
        raise CloudError("oauth_id_token_missing")
    if previous and identity["sub"] != previous["subject"]:
        raise CloudError("oauth_account_mismatch")
    scope = tokens.get("scope")
    if scope is None and previous is not None:
        scopes = previous["scopes"]
    elif isinstance(scope, str):
        scopes = scope.split()
    else:
        raise CloudError("oauth_scopes_missing")
    if "openid" not in scopes:
        raise CloudError("oauth_scopes_missing")
    if "chatgpt.tokens.use.direct" in scopes:
        if "resource.invoke" not in scopes:
            raise CloudError("oauth_scopes_missing")
        if (
            not isinstance(tokens.get("access_token"), str)
            or not tokens["access_token"]
            or expires is None
        ):
            raise CloudError("oauth_invalid_token_response")
        if "offline_access" in scopes and (
            not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]
        ):
            raise CloudError("oauth_invalid_token_response")
    return {
        "client_id": client,
        "subject": identity["sub"],
        "id_token": id_token,
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + (expires or 0),
        "scopes": scopes,
        "email": identity.get("email", (previous or {}).get("email")),
        "state": "signed_in",
    }


class ChatGptAuth:
    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    def status(self) -> dict[str, Any]:
        with self.store.locked() as database:
            return {
                "active": database["active"],
                "accounts": [
                    {
                        "label": label,
                        "email": row.get("email"),
                        "state": row["state"],
                        "plan_enabled": "chatgpt.tokens.use.direct" in row.get("scopes", []),
                        "expired": row.get("expires_at", 0) <= time.time(),
                    }
                    for label, row in database["accounts"].items()
                ],
            }

    def select(self, label: str) -> None:
        with self.store.locked() as database:
            row = database["accounts"].get(label)
            if not row or row["state"] != "signed_in":
                raise CloudError("oauth_sign_in_required")
            database["active"] = label
            self.store.save(database)

    def access_token(self, *, force: bool = False, label: str | None = None) -> str:
        with self.store.locked() as database:
            selected = label or database["active"]
            row = database["accounts"].get(selected)
            if not row or row["state"] != "signed_in":
                raise CloudError("oauth_sign_in_required")
            if "chatgpt.tokens.use.direct" not in row.get("scopes", []):
                raise CloudError("oauth_plan_permission_missing")
            if force or row["expires_at"] <= time.time() + 60:
                if not row.get("refresh_token"):
                    raise CloudError("oauth_session_not_renewable")
                try:
                    tokens = cloud_json(
                        TOKEN,
                        form={
                            "grant_type": "refresh_token",
                            "client_id": row["client_id"],
                            "refresh_token": row["refresh_token"],
                            "resource": API,
                        },
                    )
                    updated = token_record(tokens, row["client_id"], previous=row)
                except CloudError as exc:
                    if exc.code in TERMINAL_REFRESH:
                        self._clear(row)
                        self.store.save(database)
                    raise
                database["accounts"][selected] = row = updated
                self.store.save(database)
            if "chatgpt.tokens.use.direct" not in row["scopes"]:
                raise CloudError("oauth_plan_permission_missing")
            return str(row["access_token"])

    @staticmethod
    def _clear(row: dict[str, Any]) -> None:
        for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
            row.pop(key, None)
        row["state"] = "signed_out"

    def logout(self, label: str | None = None) -> bool:
        confirmed = False
        with self.store.locked() as database:
            selected = label or database["active"]
            row = database["accounts"].get(selected)
            if row:
                if row.get("refresh_token"):
                    for attempt in range(3):
                        try:
                            endpoint = discovery().get("revocation_endpoint")
                            if not isinstance(endpoint, str):
                                raise CloudError("oauth_revocation_endpoint_missing")
                            cloud_json(
                                endpoint,
                                form={
                                    "token": row["refresh_token"],
                                    "token_type_hint": "refresh_token",
                                    "client_id": row["client_id"],
                                },
                                empty=True,
                            )
                            confirmed = True
                            break
                        except CloudError as exc:
                            if exc.code not in {"connection_failed", "timeout"} and (
                                exc.http_status is None or exc.http_status < 500
                            ):
                                break
                            time.sleep(0.25 * 2**attempt)
                self._clear(row)
                if selected == database["active"]:
                    database["active"] = None
                self.store.save(database)
        return confirmed

    def login(
        self, label: str | None = None, *, timeout: float = 300, consent: bool = False
    ) -> str:
        # Only a genuinely user-invoked login opens a browser. No URLs/tokens in diagnostics.
        try:
            import jwt  # noqa: F401
        except ImportError:
            raise CloudError("chatgpt_extra_required") from None
        with self.store.locked() as database:
            previous = database["accounts"].get(label) if label else None
            if label and previous is None:
                raise CloudError("oauth_account_not_found")
            host = database["host_id"]
            self.store.save(database)
        callback: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.settimeout(min(timeout, 5))
                super().setup()

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                if parts.path != "/auth/callback" or len(self.path) > 16_384:
                    self.send_error(404)
                    return
                callback.append(parts.query)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                self.wfile.write(b"Return to Language Review to see the sign-in result.")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = min(timeout, 1)
            attempt = LoginAttempt(
                host,
                f"http://127.0.0.1:{server.server_port}/auth/callback",
                client=previous["client_id"] if previous else "dynamic_agent_client",
                deadline=time.monotonic() + timeout,
            )
            for retry in range(2):
                callback.clear()
                if not webbrowser.open(attempt.url(previous, consent)):
                    raise CloudError("oauth_browser_unavailable")
                while not callback and time.monotonic() < attempt.deadline:
                    server.handle_request()
                if not callback:
                    raise CloudError("oauth_login_timeout")
                try:
                    client, tokens = attempt.exchange(callback[0])
                    break
                except CloudError as exc:
                    if exc.code != "invalid_grant" or retry:
                        raise
                    # A new code, state/nonce/PKCE, same issued client and callback.
                    attempt = LoginAttempt(
                        host,
                        attempt.redirect_uri,
                        client=attempt.client,
                        deadline=time.monotonic() + timeout,
                    )
        record = token_record(tokens, client, previous=previous, nonce=attempt.nonce)
        with self.store.locked() as database:
            if previous is not None and database["accounts"].get(label) != previous:
                raise CloudError("oauth_session_changed")
            existing = next(
                (key for key, row in database["accounts"].items() if row["client_id"] == client),
                None,
            )
            if existing and label != existing:
                raise CloudError("oauth_registration_already_saved")
            selected = label or "account-" + uuid4().hex[:12]
            database["accounts"][selected] = record
            database["active"] = selected
            self.store.save(database)
        return selected
