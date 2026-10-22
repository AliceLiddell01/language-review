from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from support import create_junction
from test_analysis import Provider
from test_project_config import project

from language_review.check import check_paths
from language_review.cli import main
from language_review.config import load_config
from language_review.engine_llm import LlmEngine
from language_review.errors import ConfigurationError
from language_review.extraction import extract_paths
from language_review.project_config import FILENAME, load_project, path_profile, project_mapping
from language_review.review_report import review_document
from language_review.scanner import scan_paths

POLICY = """schema_version: 1
default_profile: base
profiles:
  base:
    engines: [llm]
  docs:
    engines: [llm]
    scan: {max_file_size: 10000}
    extract:
      header_language: c
      message_calls: [notice]
      selectors: {'.json': ['$.description']}
prompts:
  primary: policy/prompt.md
scan: {max_file_size: 20000, respect_gitignore: false}
extract:
  message_calls: [warn]
  selectors: {'.json': ['$.title'], '.yaml': ['$.text']}
path_rules:
  - {path: 'docs/**', profile: docs}
"""
PRIVATE = "Закрытая инструкция, не являющаяся проверяемым документом."


def policy_project(root: Path) -> Path:
    project(root, POLICY)
    (root / ".git").mkdir()
    (root / "policy").mkdir()
    (root / "policy/prompt.md").write_text(PRIVATE, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/text.md").write_text("Обычный русский текст.", encoding="utf-8")
    return root


def assert_isolation(target: Path, follow: bool, expected: set[str]) -> None:
    config = load_config()
    overrides = {"follow_symlinks": follow}
    scanned = scan_paths([str(target)], config, cli_overrides=overrides)
    assert scanned.status == "ok"
    assert {record.path for record in scanned.files if record.status == "selected"} == expected
    extracted = extract_paths([str(target)], config, cli_overrides=overrides)
    assert extracted.error_count == 0
    assert PRIVATE not in json.dumps(
        [fragment.text for doc in extracted.documents for fragment in doc.fragments],
        ensure_ascii=False,
    )
    provider = Provider()
    checked = check_paths(
        [str(target)],
        config,
        cli_overrides=overrides,
        engine_factory=lambda _name, settings: LlmEngine(settings, provider),
    )
    sent = [payload for _system, payload in provider.calls]
    assert checked.status == "clean"
    assert PRIVATE not in json.dumps(sent, ensure_ascii=False)
    assert PRIVATE not in json.dumps(review_document(checked), ensure_ascii=False)
    assert bool(provider.calls) == bool(expected)


@pytest.mark.parametrize("follow", [False, True])
def test_policy_files_never_become_source(
    tmp_path: Path, isolated_appdata: Path, follow: bool
) -> None:
    root = policy_project(tmp_path / "repo")
    for target in (root / FILENAME, root / "policy/prompt.md"):
        assert_isolation(target, follow, set())
    assert_isolation(root, follow, {"docs/text.md"})


@pytest.mark.parametrize("follow", [False, True])
@pytest.mark.parametrize(
    "kind", ["file", "directory", "junction", "hardlink", "configuration", "configuration_hardlink"]
)
def test_policy_aliases_never_become_source(
    tmp_path: Path, isolated_appdata: Path, follow: bool, kind: str
) -> None:
    if kind == "junction" and os.name != "nt":
        pytest.skip("Соединения каталогов относятся к Windows.")
    root = policy_project(tmp_path / "repo")
    alias = root / ("alias" if kind in {"directory", "junction"} else "alias.md")
    plain = root / "plain.md"
    try:
        if kind in {"hardlink", "configuration_hardlink"}:
            os.link(
                root / (FILENAME if kind == "configuration_hardlink" else "policy/prompt.md"), alias
            )
        elif kind == "junction":
            create_junction(alias, root / "policy")
        else:
            alias.symlink_to(
                root
                / (
                    "policy"
                    if kind == "directory"
                    else FILENAME
                    if kind == "configuration"
                    else "policy/prompt.md"
                ),
                target_is_directory=kind == "directory",
            )
    except OSError as exc:
        pytest.skip(f"ОС не разрешает создание ссылок: {exc}")
    before = {p: p.read_bytes() for p in (root / FILENAME, root / "policy/prompt.md")}
    assert_isolation(alias, follow, set())
    assert_isolation(root, follow, {"docs/text.md"})
    if kind in {"hardlink", "configuration_hardlink", "junction"}:
        os.link(root / "docs/text.md", plain)
        assert_isolation(plain, follow, {"plain.md"})
    else:
        plain.symlink_to(root / "docs/text.md")
        assert_isolation(plain, follow, {"text.md"} if follow else set())
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("docs", "docs/**"),
        ("docs/**", "docs"),
        ("docs", "docs"),
        ("docs/**", "docs/**"),
        ("docs/**", "docs/sub/**"),
        ("docs/sub", "docs/**"),
        ("docs/**", "docs/sub/file.md"),
    ],
)
def test_path_rule_overlap_rejected_before_engine(
    tmp_path: Path, isolated_appdata: Path, first: str, second: str
) -> None:
    root = project(
        tmp_path / "repo",
        POLICY.split("path_rules:")[0]
        + f"""path_rules:
  - {{path: '{first}', profile: base}}
  - {{path: '{second}', profile: docs}}
""",
    )
    (root / "policy").mkdir()
    (root / "policy/prompt.md").write_text(PRIVATE, encoding="utf-8")
    calls: list[str] = []
    with pytest.raises(ConfigurationError, match="конфликт правил"):
        check_paths([str(root)], load_config(), engine_factory=lambda name, _: calls.append(name))
    assert calls == []
    assert not (root / "docs").exists()


@pytest.mark.parametrize(
    ("first", "second"),
    [("docs/**", "docs2/**"), ("docs", "docs/sub/**"), ("docs/a.md", "docs/b.md")],
)
def test_disjoint_path_rules(
    tmp_path: Path, isolated_appdata: Path, first: str, second: str
) -> None:
    root = policy_project(tmp_path / "repo")
    (root / FILENAME).write_text(
        POLICY.split("path_rules:")[0]
        + f"""path_rules:
  - {{path: '{first}', profile: base}}
  - {{path: '{second}', profile: docs}}
""",
        encoding="utf-8",
    )
    assert len(load_project(load_config(), root).path_rules) == 2


@pytest.mark.parametrize("exact", ["docs", "docs/**", "docs/sub/**"])
def test_distinct_priority_path_rules(tmp_path: Path, isolated_appdata: Path, exact: str) -> None:
    root = policy_project(tmp_path / "repo")
    (root / FILENAME).write_text(
        POLICY.split("path_rules:")[0]
        + f"""path_rules:
  - {{path: 'docs/**', profile: base, priority: 0}}
  - {{path: '{exact}', profile: docs, priority: 1}}
""",
        encoding="utf-8",
    )
    config = load_project(load_config(), root)
    target = root / exact.removesuffix("/**")
    assert path_profile(config, target, directory=True) == "docs"


@pytest.mark.parametrize("kind", ["directory", "file", "default", "forced"])
@pytest.mark.parametrize("output", ["json", "human"])
def test_config_show_effective_sections_and_directory_routing(
    tmp_path: Path, isolated_appdata: Path, capsys: Any, kind: str, output: str
) -> None:
    root = policy_project(tmp_path / "repo")
    target = root if kind == "default" else root / ("docs/text.md" if kind == "file" else "docs")
    forced = "base" if kind == "forced" else None
    expected = "base" if kind in {"default", "forced"} else "docs"
    arguments = ["config", "show", str(target), "--format", output]
    if forced:
        arguments += ["--profile", forced]
    assert main(arguments) == 0
    text = capsys.readouterr().out
    value = json.loads(text[text.index("{") :])
    config = load_project(load_config(), target)
    assert value == json.loads(json.dumps(project_mapping(config, target, forced)))
    assert value["selected_profile"] == expected
    assert value["path_rule"] == ("docs/**" if expected == "docs" else None)
    scanned = scan_paths([str(target)], load_config(), requested_profile=forced)
    assert scanned.roots[0]["profile"] == expected
    assert {f.profile for f in scanned.files if f.status == "selected"} == (
        {"docs"} if kind == "default" else {expected}
    )
    assert value["scan"]["max_file_size"] == {
        "value": 10000 if expected == "docs" else 20000,
        "source": f"{FILENAME}:profiles.docs.scan" if expected == "docs" else FILENAME,
    }
    assert value["scan"]["respect_gitignore"] == {"value": False, "source": FILENAME}
    assert value["scan"]["follow_symlinks"] == {"value": False, "source": "по умолчанию"}
    extract = value["extract"]
    assert extract["header_language"] == {
        "value": "c" if expected == "docs" else "auto",
        "source": f"{FILENAME}:profiles.docs.extract" if expected == "docs" else "по умолчанию",
    }
    assert extract["message_calls"]["value"] == (
        ["warn", "notice"] if expected == "docs" else ["warn"]
    )
    assert extract["message_calls"]["source"] == (
        f"{FILENAME}; {FILENAME}:profiles.docs.extract" if expected == "docs" else FILENAME
    )
    assert extract["selectors"][".json"] == {
        "value": ["$.description"] if expected == "docs" else ["$.title"],
        "source": f"{FILENAME}:profiles.docs.extract" if expected == "docs" else FILENAME,
    }
    assert extract["selectors"][".yaml"] == {"value": ["$.text"], "source": FILENAME}
    assert value["check"]["engines"]["value"] == ["llm"]
    assert value["check"]["engines"]["source"] == f"{FILENAME}:profiles.{expected}"
    assert PRIVATE not in text and str(root) not in text
