"""HTTP JSON для локальных языковых движков без прокси, перенаправлений и неограниченных ответов."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from language_review import __version__
from language_review.config import is_loopback_host, redact_url

MAX_RESPONSE_BYTES = 8_000_000


class EngineHttpError(Exception):
    """Сбой транспорта или протокола; `code` — машинный код без пользовательских данных."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


# Прокси из окружения не используются: тексты исходников не должны уходить через посредника,
# а адрес loopback через системный прокси иногда отвечает чужим кодом (например, 503).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: Any


def endpoint_origin(url: str) -> str:
    return "loopback" if is_loopback_host(urlsplit(url).hostname) else "remote"


def safe_endpoint(url: str) -> str:
    return redact_url(url)


def join_url(base: str, path: str) -> str:
    parts = urlsplit(base)
    joined = parts.path.rstrip("/") + "/" + path.lstrip("/")
    return urlunsplit(parts._replace(path=joined))


def request_json(
    url: str,
    *,
    payload: Any = None,
    form: dict[str, str] | None = None,
    timeout: float,
    max_bytes: int = MAX_RESPONSE_BYTES,
    accept_statuses: tuple[int, ...] = (200,),
) -> HttpReply:
    """Выполнить запрос и разобрать JSON; любые отклонения превращаются в EngineHttpError."""
    headers = {"User-Agent": f"language-review/{__version__}", "Accept": "application/json"}
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(max_bytes + 1)
        except OSError:
            raw = b""
        if status not in accept_statuses:
            code = "redirect_refused" if 300 <= status < 400 else f"http_{status}"
            raise EngineHttpError(code, f"HTTP {status}", http_status=status) from exc
    except TimeoutError as exc:
        raise EngineHttpError("timeout", "превышено время ожидания ответа") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise EngineHttpError("timeout", "превышено время ожидания ответа") from exc
        raise EngineHttpError("connection_failed", "сервис недоступен") from exc
    except http.client.HTTPException as exc:
        raise EngineHttpError("protocol_error", "ответ не соответствует протоколу HTTP") from exc
    except (OSError, ValueError) as exc:
        raise EngineHttpError("connection_failed", "сервис недоступен") from exc
    if status not in accept_statuses:
        raise EngineHttpError(f"http_{status}", f"HTTP {status}", http_status=status)
    if len(raw) > max_bytes:
        raise EngineHttpError("response_too_large", "ответ превышает допустимый размер")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineHttpError("invalid_json", "ответ не является JSON") from exc
    return HttpReply(status, body)
