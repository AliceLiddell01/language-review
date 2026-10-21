from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from support.project_inputs import scan_fixture_paths as scan_paths

from language_review.config import load_config
from language_review.report import scan_json


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mixed_tree_is_deterministic_and_read_only(tmp_path: Path, isolated_appdata: Path) -> None:
    root = tmp_path / "Проект с пробелами"
    (root / "node_modules" / "vendor").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    files = {
        root / "Привет.md": b"\xef\xbb\xbf" + "Привет\r\nМир\r\n".encode(),
        root / "src.rs": b"fn main() {}\n",
        root / "app.cs": b"class App {}\n",
        root / "native.cpp": b"int main() {}\n",
        root / "ignored.txt": b"ignore me\n",
        root / "unknown.pdf": b"%PDF-1.7",
        root / "binary.md": b"\x00\x01\x02",
        root / "node_modules" / "vendor" / "dependency.rs": b"fn vendor() {}\n",
        root / "docs" / "empty.rst": b"",
    }
    for path, data in files.items():
        path.write_bytes(data)
    before = {path: _hash(path) for path in files}
    before[root / ".gitignore"] = _hash(root / ".gitignore")

    config = load_config()
    first = scan_paths([str(root)], config)
    second = scan_paths([str(root)], config)

    assert scan_json(first) == scan_json(second)
    records = {record.path: record for record in first.files}
    assert records["Привет.md"].status == "selected"
    assert records["Привет.md"].encoding == "utf-8"
    assert records["Привет.md"].bom == "utf-8"
    assert records["Привет.md"].line_endings == "crlf"
    assert records["src.rs"].file_type == "rust_source"
    assert records["app.cs"].file_type == "csharp_source"
    assert records["native.cpp"].file_type == "cpp_source"
    assert records["docs/empty.rst"].line_endings == "none"
    assert records["ignored.txt"].reason == "gitignore"
    assert records["unknown.pdf"].reason == "unsupported_extension"
    assert records["binary.md"].reason == "binary_content"
    assert records["node_modules"].reason == "excluded_directory"
    assert all(_hash(path) == digest for path, digest in before.items())


def test_explicit_file_empty_directory_missing_and_duplicate(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    source = tmp_path / "одиночный.rs"
    source.write_bytes(b"// file\n")
    config = load_config()

    empty_result = scan_paths([str(empty)], config)
    file_result = scan_paths([str(source)], config)
    duplicate_result = scan_paths([str(source), str(source)], config)
    missing_result = scan_paths([str(tmp_path / "нет.txt")], config)

    assert empty_result.files == []
    assert empty_result.status == "ok"
    assert file_result.selected_count == 1
    assert duplicate_result.skipped_count == 1
    assert any(record.reason == "duplicate_argument" for record in duplicate_result.files)
    assert missing_result.input_errors
    assert missing_result.status == "input_error"
    assert missing_result.error_count == 1


def test_gitignore_negation_nested_rules_and_parent_repository(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    repository = tmp_path / "repo"
    nested = repository / "docs" / "nested"
    nested.mkdir(parents=True)
    (repository / ".git").mkdir()
    (repository / ".gitignore").write_text("*.tmp\n!keep.tmp\n", encoding="utf-8")
    (repository / "drop.tmp").write_text("ignored", encoding="utf-8")
    (repository / "keep.tmp").write_text("included", encoding="utf-8")
    (nested / "nested.md").write_text("text", encoding="utf-8")
    (nested / ".gitignore").write_text("nested.md\n", encoding="utf-8")

    result = scan_paths(
        [str(nested), str(repository / "drop.tmp"), str(repository / "keep.tmp")],
        load_config(),
        cli_overrides={"extensions_add": (".tmp",)},
    )
    records = {(record.root, record.path): record for record in result.files}

    assert records[(str(nested), "nested.md")].reason == "gitignore"
    assert records[(str(repository / "drop.tmp"), "drop.tmp")].reason == "gitignore"
    assert records[(str(repository / "keep.tmp"), "keep.tmp")].status == "selected"


def test_invalid_gitignore_is_reported_as_partial_error(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = tmp_path / "broken-ignore"
    root.mkdir()
    (root / ".gitignore").write_bytes(b"\xff")
    (root / "text.md").write_text("text", encoding="utf-8")

    result = scan_paths([str(root)], load_config())

    assert result.error_count == 1
    assert result.status == "partial"
    assert result.roots[0]["status"] == "partial"


def test_cli_overrides_extensions_and_patterns(tmp_path: Path, isolated_appdata: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "one.rs").write_text("rust", encoding="utf-8")
    (root / "two.md").write_text("markdown", encoding="utf-8")
    (root / "hide.rs").write_text("hidden", encoding="utf-8")

    result = scan_paths(
        [str(root)],
        load_config(),
        cli_overrides={"only_extensions": (".md", ".rs"), "exclude_add": ("hide.*",)},
    )
    records = {record.path: record for record in result.files}

    assert records["two.md"].status == "selected"
    assert records["one.rs"].status == "selected"
    assert records["hide.rs"].reason == "excluded_pattern"


def test_links_are_confined_and_cycles_are_skipped(tmp_path: Path, isolated_appdata: Path) -> None:
    import os
    import shutil
    import subprocess

    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "inside.md").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.md").write_text("outside", encoding="utf-8")
    outside_link = root / "outside-link"
    cycle_link = child / "cycle"

    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is not None and os.name == "nt":
        environment = os.environ.copy()
        environment["LANGUAGE_REVIEW_OUTSIDE_LINK"] = str(outside_link)
        environment["LANGUAGE_REVIEW_OUTSIDE_TARGET"] = str(outside)
        environment["LANGUAGE_REVIEW_CYCLE_LINK"] = str(cycle_link)
        environment["LANGUAGE_REVIEW_CYCLE_TARGET"] = str(root)
        command = (
            "$ErrorActionPreference = 'Stop'; "
            "New-Item -ItemType Junction -Path $env:LANGUAGE_REVIEW_OUTSIDE_LINK "
            "-Target $env:LANGUAGE_REVIEW_OUTSIDE_TARGET | Out-Null; "
            "New-Item -ItemType Junction -Path $env:LANGUAGE_REVIEW_CYCLE_LINK "
            "-Target $env:LANGUAGE_REVIEW_CYCLE_TARGET | Out-Null"
        )
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"Создание Windows junction недоступно: {completed.stderr}")
        assert outside_link.is_junction()
        assert cycle_link.is_junction()
    else:
        try:
            outside_link.symlink_to(outside, target_is_directory=True)
            cycle_link.symlink_to(root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Создание символических ссылок недоступно: {exc}")

    result = scan_paths(
        [str(root)],
        load_config(),
        cli_overrides={"follow_symlinks": True},
    )
    records = {record.path: record for record in result.files}

    assert records["outside-link"].reason == "symlink_outside_root"
    assert records["child/cycle"].reason == "directory_cycle_or_duplicate"
    assert records["child/inside.md"].status == "selected"
    assert not any(record.path.startswith("outside-link/") for record in result.files)


def test_project_marker_and_path_profile_select_expected_settings(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "crates").mkdir()
    (repository / "docs").mkdir()
    (repository / "Cargo.toml").write_text("[package]", encoding="utf-8")
    (repository / "crates" / "lib.rs").write_text("fn lib() {}", encoding="utf-8")
    (repository / "docs" / "guide.md").write_text("guide", encoding="utf-8")
    config_path = tmp_path / "profiles.toml"
    config_path.write_text(
        """
[profiles.rust]
priority = 10
project_markers = ["Cargo.toml"]
[profiles.rust.scan]
extensions = [".rs"]

[profiles.docs]
priority = 20
match_paths = ["docs/**"]
[profiles.docs.scan]
extensions = [".md"]
""",
        encoding="utf-8",
    )

    result = scan_paths([str(repository)], load_config(config_path))
    records = {record.path: record for record in result.files}

    assert records["crates/lib.rs"].status == "selected"
    assert records["crates/lib.rs"].profile == "rust"
    assert records["docs/guide.md"].status == "selected"
    assert records["docs/guide.md"].profile == "docs"
