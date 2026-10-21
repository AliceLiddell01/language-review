"""Публичный OAuth HTTP/SSE через системный прокси, без API-ключей и перенаправлений."""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from language_review.engine_http import MAX_RESPONSE_BYTES, EngineHttpError, _NoRedirect

API = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
# Облачный HTTPS следует настройкам сети ОС; TLS и список разрешённых адресов действуют в туннеле.
# Локальные движки по-прежнему обходят прокси через собственный engine_http.
_OPENER = urllib.request.build_opener(_NoRedirect)
_CODE = re.compile(r"[a-zA-Z0-9_.-]{1,120}\Z")


class CloudError(EngineHttpError):
    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        shape: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(code, code, http_status=status)
        self.request_id = (
            request_id if isinstance(request_id, str) and _CODE.fullmatch(request_id) else None
        )
        self.shape = shape
        self.param = param if isinstance(param, str) and _CODE.fullmatch(param) else None

    def metadata(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "http_status": self.http_status,
            "request_id": self.request_id,
            "body_shape": self.shape,
            "param": self.param,
        }


def error_from(body: Any, status: int | None = None, request_id: str | None = None) -> CloudError:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(body, dict) and body.get("type") == "error" and "code" in body:
        error = body  # Поля ошибки Responses SSE находятся на верхнем уровне события.
    code = error.get("code") or error.get("type") if isinstance(error, dict) else None
    if isinstance(error, str):  # Адрес выдачи токенов OAuth.
        code = error
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        code = f"http_{status}" if status else "response_failed"
    shape = (
        "error" if error else "detail" if isinstance(body, dict) and "detail" in body else "other"
    )
    return CloudError(
        code,
        status=status,
        request_id=request_id,
        shape=shape,
        param=error.get("param") if isinstance(error, dict) else None,
    )


@contextmanager
def open_response(
    url: str,
    *,
    token: str | None = None,
    payload: Any = None,
    form: dict[str, str] | None = None,
    timeout: float = 30,
) -> Iterator[Any]:
    # Вызывающий код использует константные адреса API/авторизации либо проверенные метаданные.
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"auth.openai.com", "api.openai.com"}
        or (parts.username or parts.password or parts.port not in {None, 443} or parts.fragment)
    ):
        raise CloudError("unsafe_cloud_endpoint")
    headers = {"Accept": "text/event-stream" if payload else "application/json"}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with _OPENER.open(
            urllib.request.Request(url, data=data, headers=headers), timeout=timeout
        ) as r:
            yield r
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        try:
            body = json.loads(raw) if len(raw) <= MAX_RESPONSE_BYTES else None
        except ValueError, UnicodeError, RecursionError:
            body = None
        raise error_from(body, exc.code, exc.headers.get("x-request-id")) from None
    except TimeoutError:
        raise CloudError("timeout") from None
    except urllib.error.URLError, OSError, http.client.HTTPException:
        raise CloudError("connection_failed") from None


def cloud_json(
    url: str,
    *,
    token: str | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30,
    empty: bool = False,
) -> Any:
    with open_response(url, token=token, form=form, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CloudError("response_too_large")
    if empty and not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError, UnicodeError, RecursionError:
        raise CloudError("invalid_json") from None


def stream_response(payload: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    with open_response(
        API + "/responses", payload=payload, token=token, timeout=timeout
    ) as response:
        # SIWC может опускать Content-Type: в этом случае доверяем только полностью
        # разобранному SSE с response.completed, а не пустому/частичному тексту.
        if (
            response.headers.get("Content-Type") is not None
            and response.headers.get_content_type() != "text/event-stream"
        ):
            raise CloudError("sse_content_type")
        data: list[str] = []
        text: list[str] = []
        for raw in _sse_lines(response, deadline):
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeError:
                raise CloudError("sse_invalid_utf8") from None
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                try:
                    event = json.loads("\n".join(data))
                except ValueError, RecursionError:
                    raise CloudError("sse_invalid_json") from None
                data = []
                if not isinstance(event, dict):
                    raise CloudError("sse_protocol_error")
                kind = event.get("type")
                if not isinstance(kind, str):
                    raise CloudError("sse_protocol_error")
                if kind in {"response.failed", "error"}:
                    raise error_from(
                        event.get("response", event),
                        request_id=response.headers.get("x-request-id"),
                    )
                if kind == "response.incomplete":
                    raise CloudError("response_incomplete")
                if kind == "response.output_text.delta":
                    if not isinstance(event.get("delta"), str):
                        raise CloudError("sse_protocol_error")
                    text.append(event["delta"])
                if kind == "response.completed":
                    result = event.get("response")
                    if not isinstance(result, dict) or result.get("status") != "completed":
                        raise CloudError("sse_protocol_error")
                    # Не считаем отказ модели или пустой вывод чистым JSON-анализом.
                    return {
                        "content": "".join(text),
                        "usage": result.get("usage"),
                        "model": result.get("model"),
                        "reasoning": result.get("reasoning"),
                    }
        raise CloudError("sse_missing_completed")


def _sse_lines(response: Any, deadline: float) -> Iterator[bytes]:
    """read1 не ждёт заполнения буфера; общий срок ограничивает и медленную передачу SSE."""
    pending = b""
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CloudError("timeout")
        # HTTPResponse предоставляет буфер fp; таймаут сокета уменьшается с общим бюджетом.
        raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if raw_socket is not None:
            raw_socket.settimeout(remaining)
        chunk = response.read1(4096)
        if time.monotonic() >= deadline:
            raise CloudError("timeout")
        if not chunk:
            return
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise CloudError("response_too_large")
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line
