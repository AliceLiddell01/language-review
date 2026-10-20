"""SIWC Responses transport, только вручную выбранные разрешённые модели."""

from __future__ import annotations

from typing import Any

from language_review.chatgpt_auth import ChatGptAuth
from language_review.chatgpt_http import API, CloudError, cloud_json, stream_response
from language_review.check_models import ProbeResult
from language_review.config import CHATGPT_EFFORTS, CheckSettings, validate_check_settings
from language_review.engine_llm import ProviderReply, provider_timeout


def models(auth: ChatGptAuth, label: str) -> list[dict[str, Any]]:
    response = cloud_json(API + "/models", token=auth.access_token(label=label))
    rows = response.get("models") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise CloudError("model_catalog_invalid")
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get("visibility") != "list":
            continue
        slug, name = row.get("slug"), row.get("display_name")
        if not isinstance(slug, str) or not isinstance(name, str):
            raise CloudError("model_catalog_invalid")
        result.append({"slug": slug, "display_name": name, "allowed": slug in CHATGPT_EFFORTS})
    return result


class ChatGptProvider:
    name = "chatgpt"
    endpoint = API

    def __init__(self, settings: CheckSettings, auth: ChatGptAuth | None = None) -> None:
        validate_check_settings(settings)
        self.settings = settings
        self.deadline: float | None = None
        self.model: str | None = settings.llm_chatgpt_model
        self.auth = auth
        self.label: str | None = None
        self.metadata: dict[str, Any] = {
            "reasoning_effort": settings.llm_chatgpt_effort,
            "effective_reasoning_effort": None,
            "effort_capability": "not_probed",
            "subscription_usage": None,
            "subscription_cost": None,
        }

    def probe(self) -> ProbeResult:
        if not self.settings.llm_chatgpt_allow_cloud:
            try:
                status = (self.auth or ChatGptAuth()).status()
                self.metadata["authorization"] = (
                    "signed_in" if status["active"] else "sign_in_required"
                )
            except CloudError, OSError:
                self.metadata["authorization"] = "storage_unavailable"
            return ProbeResult("unavailable", self.metadata, ["cloud_consent_required"])
        try:
            self.auth = self.auth or ChatGptAuth()
            status = self.auth.status()
            self.label = status["active"]
            if not self.label:
                raise CloudError("oauth_sign_in_required")
            catalog = models(self.auth, self.label)
            available = [row["slug"] for row in catalog if row["allowed"]]
            self.metadata["available_models"] = available
            if self.model not in available:
                raise CloudError("selected_model_unavailable")
            self.metadata["effort_capability"] = "catalog_does_not_declare_efforts"
            return ProbeResult("available", self.metadata)
        except OSError:
            return ProbeResult("unavailable", self.metadata, ["chatgpt_storage_unavailable"])
        except CloudError as exc:
            self.metadata["last_error"] = exc.metadata()
            return ProbeResult("unavailable", self.metadata, [exc.code], exc.code)

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        if not self.settings.llm_chatgpt_allow_cloud:
            raise CloudError("cloud_consent_required")
        if self.auth is None or self.label is None:
            raise CloudError("oauth_sign_in_required")
        # Structured outputs не предполагаются разрешёнными OAuth-профилем.
        # Схема включена в инструкции; parse_response остаётся обязательной проверкой.
        import json

        payload = {
            "model": self.model,
            "store": False,
            "stream": True,
            "input": [{"role": "user", "content": user}],
            "instructions": system + "\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False),
            "reasoning": {"effort": self.settings.llm_chatgpt_effort},
        }
        try:
            result = stream_response(
                payload,
                self.auth.access_token(label=self.label),
                provider_timeout(self, self.settings),
            )
        except OSError:
            raise CloudError("chatgpt_storage_unavailable") from None
        except CloudError as exc:
            self.metadata["last_error"] = exc.metadata()
            raise
        if result["model"] != self.model:
            raise CloudError("response_model_mismatch")
        self.metadata["effort_capability"] = "request_completed"
        self.metadata["effective_model"] = result["model"]
        reasoning = result.get("reasoning")
        if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
            if reasoning["effort"] != self.settings.llm_chatgpt_effort:
                raise CloudError("response_effort_mismatch")
            self.metadata["effective_reasoning_effort"] = reasoning["effort"]
        else:
            self.metadata["effort_capability"] = "request_completed_without_effort_echo"
        usage = result["usage"] if isinstance(result["usage"], dict) else {}
        return ProviderReply(
            result["content"], False, usage.get("input_tokens"), usage.get("output_tokens")
        )
