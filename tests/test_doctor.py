from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from language_review.config import load_config
from language_review.doctor import doctor_exit_code, run_doctor


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        return


def test_doctor_checks_paths_programs_and_local_http(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config_path = tmp_path / "doctor.toml"
        config_path.write_text(
            f"""
[doctor]
programs = ["definitely-not-installed-language-review-tool"]
[doctor.http_services]
local = "http://127.0.0.1:{server.server_port}/health?token=secret"
""",
            encoding="utf-8",
        )
        existing = tmp_path / "input.md"
        existing.write_text("text", encoding="utf-8")

        result = run_doctor(load_config(config_path), [str(existing)])

        assert result["paths"][0]["status"] == "available"
        assert result["programs"][0]["status"] == "missing"
        assert result["http_services"][0]["status"] == "reachable"
        assert "secret" not in json.dumps(result)
        assert doctor_exit_code(result) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_doctor_missing_path_has_input_exit_code(tmp_path: Path, isolated_appdata: Path) -> None:
    result = run_doctor(load_config(), [str(tmp_path / "absent")])

    assert result["paths"][0]["status"] == "missing"
    assert result["input_errors"] is True
    assert doctor_exit_code(result) == 2


def test_doctor_reports_selected_engines_without_secrets(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    config_path = tmp_path / "doctor.toml"
    config_path.write_text(
        """
[check]
engines = ["languagetool", "vale"]
[check.languagetool]
endpoint = "http://127.0.0.1:9/?token=secret"
[check.vale]
program = "definitely-missing-vale-binary"
""",
        encoding="utf-8",
    )

    result = run_doctor(load_config(config_path), [])

    assert [engine["name"] for engine in result["engines"]] == ["languagetool", "vale"]
    assert {engine["status"] for engine in result["engines"]} == {"unavailable"}
    assert result["engines"][0]["details"]["endpoint_origin"] == "loopback"
    assert result["engines"][1]["diagnostics"] == ["program_not_found"]
    assert result["check"]["engines"] == ["languagetool", "vale"]
    assert "secret" not in json.dumps(result, ensure_ascii=False)
    assert doctor_exit_code(result) == 3
