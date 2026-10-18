from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run(args: list[str], appdata: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["APPDATA"] = str(appdata)
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


def test_version_module_entrypoint_and_config_show(tmp_path: Path, isolated_appdata: Path) -> None:
    version = _run(["--version"], isolated_appdata)
    assert version.returncode == 0
    assert version.stdout.strip() == "language-review 0.1.0"

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
    assert actual == expected
