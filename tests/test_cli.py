from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def _project_version() -> str:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    with pyproject.open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _run(args: list[str], appdata: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["APPDATA"] = str(appdata)
    environment["XDG_CONFIG_HOME"] = str(appdata)
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "language_review", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def test_json_stdout_and_report_file_preserve_sources(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = tmp_path / "каталог"
    root.mkdir()
    source = root / "Привет.md"
    original = b"\xef\xbb\xbf" + "текст\r\n".encode()
    source.write_bytes(original)

    process = _run(["scan", str(root), "--format", "json"], isolated_appdata)

    assert process.returncode == 0
    assert process.stderr == ""
    document = json.loads(process.stdout)
    assert document["schema_version"] == "1.0"
    assert document["summary"]["selected"] == 1
    assert document["files"][0]["path"] == "Привет.md"
    assert "findings" not in document
    assert source.read_bytes() == original

    report = tmp_path / "report.json"
    saved = _run(
        ["scan", str(root), "--format", "json", "--output", str(report)],
        isolated_appdata,
    )
    assert saved.returncode == 0
    assert saved.stdout == ""
    assert report.is_file()
    assert json.loads(report.read_text(encoding="utf-8"))["summary"]["selected"] == 1
    assert source.read_bytes() == original


def test_json_missing_input_has_exit_two_and_remains_valid(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    process = _run(
        ["scan", str(tmp_path / "absent"), "--format", "json"],
        isolated_appdata,
    )

    assert process.returncode == 2
    document = json.loads(process.stdout)
    assert document["status"] == "input_error"
    assert document["summary"]["errors"] == 1


def test_report_cannot_overwrite_input_and_config_errors_are_clear(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "source.rs"
    source.write_bytes(b"fn main() {}\n")
    original = source.read_bytes()

    process = _run(
        ["scan", str(source), "--output", str(source), "--format", "json"],
        isolated_appdata,
    )
    assert process.returncode == 3
    assert "нельзя сохранить поверх" in process.stderr
    assert source.read_bytes() == original

    bad_config = tmp_path / "bad.toml"
    bad_config.write_text("[unknown]\nkey = true\n", encoding="utf-8")
    invalid = _run(
        ["--config", str(bad_config), "scan", str(source), "--format", "json"],
        isolated_appdata,
    )
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert "Ошибка конфигурации" in invalid.stderr


@pytest.mark.parametrize(
    "endpoint", ["http://[::1", "http://[xx]:8081", "http://127.0.0.1:70000", "http://[::1]:x"]
)
@pytest.mark.parametrize("command", [["check"], ["config", "show"]])
def test_invalid_service_url_is_configuration_error_not_traceback(
    tmp_path: Path, isolated_appdata: Path, endpoint: str, command: list[str]
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Текст.\n", encoding="utf-8")
    config = tmp_path / "bad-url.toml"
    config.write_text(f'[check.languagetool]\nendpoint = "{endpoint}"\n', encoding="utf-8")

    paths = [str(source)] if command == ["check"] else []
    process = _run(["--config", str(config), *command, *paths], isolated_appdata)

    assert process.returncode == 2
    assert process.stdout == ""
    assert "Ошибка конфигурации" in process.stderr
    assert "Traceback" not in process.stderr
    assert source.read_text(encoding="utf-8") == "Текст.\n"


def test_version_module_entrypoint_and_config_show(tmp_path: Path, isolated_appdata: Path) -> None:
    version = _run(["--version"], isolated_appdata)
    assert version.returncode == 0
    assert version.stdout.strip() == f"language-review {_project_version()}"

    config = _run(["config", "show", "--format", "json"], isolated_appdata)
    assert config.returncode == 0
    mapping = json.loads(config.stdout)
    assert mapping["scan"]["respect_gitignore"]["value"] is True
    assert mapping["scan"]["respect_gitignore"]["source"] == "по умолчанию"
    assert "findings" not in mapping


def test_config_show_profile_reports_selector_sources(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    user_path = isolated_appdata / "language-review" / "config.toml"
    user_path.parent.mkdir(parents=True)
    user_path.write_text('[extract.selectors]\n".json" = ["$.ui.base"]\n', encoding="utf-8")
    explicit = tmp_path / "explicit.toml"
    explicit.write_text(
        """
[profiles.readable_json]
match_paths = ["*.json"]

[profiles.readable_json.extract.selectors]
".json" = ["$.ui.message"]
".yaml" = ["$.ui.message"]
""",
        encoding="utf-8",
    )

    base = _run(["--config", str(explicit), "config", "show", "--format", "json"], isolated_appdata)
    assert base.returncode == 0
    base_selectors = json.loads(base.stdout)["extract"]["selectors"]
    assert base_selectors[".json"]["value"] == ["$.ui.base"]
    assert base_selectors[".json"]["source"].endswith(str(user_path))
    assert ".yaml" not in base_selectors

    process = _run(
        [
            "--config",
            str(explicit),
            "config",
            "show",
            "--profile",
            "readable_json",
            "--format",
            "json",
        ],
        isolated_appdata,
    )

    assert process.returncode == 0
    selectors = json.loads(process.stdout)["extract"]["selectors"]
    assert selectors[".json"]["value"] == ["$.ui.message"]
    assert selectors[".json"]["source"].endswith(str(explicit))
    assert selectors[".yaml"]["value"] == ["$.ui.message"]
    assert selectors[".yaml"]["source"].endswith(str(explicit))


def test_extract_report_example_matches_real_cli_output(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "extract"
    source = tmp_path / "sample.json"
    source.write_bytes((fixtures / "sample.json").read_bytes().replace(b"\r\n", b"\n"))
    process = _run(
        [
            "--config",
            str(fixtures / "example-config.toml"),
            "extract",
            str(source),
            "--format",
            "json",
        ],
        isolated_appdata,
    )

    assert process.returncode == 0
    assert process.stderr == ""
    actual = json.loads(process.stdout)
    expected = json.loads(
        (Path(__file__).parents[1] / "docs" / "extract-report.example.json").read_text(
            encoding="utf-8"
        )
    )
    # Версия пакета берётся из pyproject.toml и не входит в эталон поведения;
    # версия схемы отчёта остаётся частью сравнения.
    assert actual["tool"]["version"] == _project_version()
    assert isinstance(expected["tool"].pop("version"), str)
    actual["tool"].pop("version")
    assert actual == expected
