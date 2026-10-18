from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def isolated_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(appdata))
    return appdata


@dataclass
class RecordedRequest:
    method: str
    path: str
    body: Any


Responder = Callable[[RecordedRequest], tuple[int, Any]]


@dataclass
class JsonServer:
    """Локальный HTTP-сервер протокола движка для контрактных проверок отказов."""

    url: str
    requests: list[RecordedRequest] = field(default_factory=list)
    responder: Responder | None = None


def _decode_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    content_type = handler.headers.get("Content-Type") or ""
    if not raw:
        return None
    if content_type.startswith("application/json"):
        return json.loads(raw.decode("utf-8"))
    if content_type.startswith("application/x-www-form-urlencoded"):
        return dict(urllib.parse.parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
    return raw.decode("utf-8")


@pytest.fixture
def json_server() -> Iterator[JsonServer]:
    state = JsonServer(url="")

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            request = RecordedRequest(self.command, self.path, _decode_body(self))
            state.requests.append(request)
            assert state.responder is not None
            status, payload = state.responder(request)
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
