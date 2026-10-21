from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support import project_inputs

from language_review.cli import main
from language_review.config import CheckSettings, load_config
from language_review.errors import ConfigurationError
from language_review.project_config import (
    FILENAME,
    MAX_CONFIG_BYTES,
    MAX_PROMPT_BYTES,
    TEMPLATE,
    init_project,
    load_project,
    path_profile,
    project_mapping,
    repository_root,
)
from language_review.scanner import scan_paths


def project(root: Path, text: str = TEMPLATE) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / FILENAME).write_text(text, encoding="utf-8")
    return root


def test_discovery_git_worktree_subdirectory_and_foreign_cwd(
    tmp_path: Path, monkeypatch: Any, isolated_appdata: Path
) -> None:
    root = project(tmp_path / "проект с пробелами")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    nested = root / "docs"
    nested.mkdir()
    target = nested / "file.md"
    target.write_text("Текст.", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert repository_root(target) == root
    assert load_project(load_config(), target).project_root == root
    monkeypatch.chdir(nested)
    assert repository_root("file.md") == root
    worktree = project(tmp_path / "worktree")
    # Файл .git является границей, как в linked worktree; его содержимое не исполняется.
    (worktree / ".git").write_text("gitdir: ../metadata", encoding="utf-8")
    assert repository_root(worktree) == worktree
    assert (
        load_project(load_config(), worktree).project_id
        != load_project(load_config(), root).project_id
    )


def test_fixture_preparation_refuses_roots_outside_temporary_directory(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: Any
) -> None:
    root = project(tmp_path / "repo")
    (root / ".git").mkdir()
    source = root / "notes.md"
    source.write_text("Текст.", encoding="utf-8")
    policy = root / FILENAME
    original = policy.read_bytes()
    monkeypatch.setattr(
        project_inputs.tempfile, "gettempdir", lambda: str(tmp_path / "allowed-temp")
    )

    with pytest.raises(AssertionError, match="системный временный каталог"):
        project_inputs.prepare([str(source)], load_config())

    assert policy.read_bytes() == original


def test_missing_policy_never_uses_parent_or_global_profile(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    parent = project(tmp_path / "outer")
    (parent / ".git").mkdir()
    child = parent / "nested"
    (child / ".git").mkdir(parents=True)
    environment = replace(load_config(), check=CheckSettings(llm_provider="chatgpt"))
    with pytest.raises(ConfigurationError, match="обязателен"):
        scan_paths([str(child)], environment)
    nongit = tmp_path / "plain"
    nongit.mkdir()
    with pytest.raises(ConfigurationError, match="обязателен"):
        scan_paths([str(nongit)], environment)
    # Вне Git нет неявного подъёма к конфигу родительского каталога.
    project(nongit)
    sub = nongit / "sub"
    sub.mkdir()
    with pytest.raises(ConfigurationError, match="обязателен"):
        scan_paths([str(sub)], environment)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("schema_version: 2\n", "schema_version"),
        (TEMPLATE + "schema_version: 1\n", "YAML"),
        (TEMPLATE + "unknown: true\n", "неизвестные"),
        (TEMPLATE.replace("normal", "mystery"), "analysis.mode"),
        (TEMPLATE.replace("mode: normal", "strategies: [unknown]"), "strategies"),
        (
            TEMPLATE.replace("provider: llama_cpp", "provider: chatgpt\n      model: missing"),
            "model",
        ),
        (
            TEMPLATE.replace(
                "provider: llama_cpp",
                "provider: chatgpt\n      model: gpt-6.1-sol\n      reasoning_effort: none",
            ),
            "model/effort",
        ),
        (
            TEMPLATE.replace(
                "provider: llama_cpp", "provider: ollama\n      reasoning_effort: high"
            ),
            "reasoning_effort",
        ),
        (TEMPLATE.replace("provider: llama_cpp", "provider: []"), "provider"),
        (TEMPLATE.replace("provider: llama_cpp", "provider: llama_cpp\n      model: ''"), "model"),
        (
            TEMPLATE.replace("provider: llama_cpp", "provider: chatgpt\n      allow_cloud: true"),
            "неподдерживаемые",
        ),
        (
            TEMPLATE.replace(
                "provider: llama_cpp", "provider: ollama\n      endpoint: http://example.com"
            ),
            "неподдерживаемые",
        ),
        (TEMPLATE + "prompts:\n  primary: {text: ''}\n", "непустой"),
        (TEMPLATE + "prompts:\n  primary: {file: ../outside.md}\n", "относительный"),
        (TEMPLATE + "prompts:\n  primary: {file: 'C:/outside.md'}\n", "относительный"),
        (TEMPLATE + "prompts:\n  primary: {file: missing.md}\n", "недоступен"),
        (TEMPLATE + "prompts:\n  primary: {text: hi, file: p.md}\n", "ровно"),
        (TEMPLATE + "prompts:\n  strategies: {missing: {text: hi}}\n", "неизвестные"),
        (TEMPLATE + "path_rules: [{path: 'docs/**', profile: absent}]\n", "profile"),
        (
            TEMPLATE + "path_rules: [{path: 'docs/**', profile: everyday}, "
            "{path: 'docs/sub/**', profile: everyday}]\n",
            "конфликт",
        ),
        (TEMPLATE + "path_rules: [{path: '../docs/**', profile: everyday}]\n", "относительный"),
        (TEMPLATE + "path_rules: [{path: '**/*.md', profile: everyday}]\n", "допустим"),
        (TEMPLATE + "project_id: bad\n", "UUID"),
        ("!!python/object/apply:os.system ['echo no']", "теги"),
        (TEMPLATE + "extra: &alias [1, 2]\n", "anchors"),
        (TEMPLATE + "extra: " + "[" * 20 + "0" + "]" * 20, "сложности"),
    ],
)
def test_invalid_project_rejected_before_engine(
    tmp_path: Path, isolated_appdata: Path, mutation: str, match: str
) -> None:
    root = project(tmp_path / "repo", mutation)
    from language_review.check import check_paths

    calls = []
    with pytest.raises(ConfigurationError, match=match):
        check_paths([str(root)], load_config(), engine_factory=lambda *args: calls.append(args))
    assert calls == []


def test_size_limits_utf8_and_symlink_escape(tmp_path: Path, isolated_appdata: Path) -> None:
    root = project(tmp_path / "repo", "#" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigurationError, match="предел"):
        load_project(load_config(), root)
    project(root, TEMPLATE + "prompts:\n  primary: prompt.md\n")
    prompt = root / "prompt.md"
    prompt.write_bytes(b"x" * MAX_PROMPT_BYTES)
    assert load_project(load_config(), root).profiles["everyday"].check_overrides["prompt_primary"]
    prompt.write_bytes(b"x" * (MAX_PROMPT_BYTES + 1))
    with pytest.raises(ConfigurationError, match="предел"):
        load_project(load_config(), root)
    prompt.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="UTF-8"):
        load_project(load_config(), root)
    prompt.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("Текст.", encoding="utf-8")
    try:
        prompt.symlink_to(outside)
    except OSError:
        pytest.skip("ОС не разрешает создание symlink.")
    with pytest.raises(ConfigurationError, match="внутри"):
        load_project(load_config(), root)


def test_rules_priority_forced_profile_and_diagnostic_redaction(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = project(
        tmp_path / "repo",
        """schema_version: 1
default_profile: local
profiles:
  local:
    llm: {provider: ollama, model: local-model}
  docs:
    llm: {provider: chatgpt, model: gpt-6.1-sol, reasoning_effort: high}
prompts:
  primary: {text: 'Приватные инструкции проекта.'}
path_rules:
  - {path: 'docs/**', profile: docs, priority: 1}
  - {path: 'docs/local.md', profile: local, priority: 2}
""",
    )
    (root / ".git").mkdir()
    (root / "docs").mkdir()
    target = root / "docs/file.md"
    target.write_text("Частный текст.", encoding="utf-8")
    local = root / "docs/local.md"
    local.write_text("Локальный текст.", encoding="utf-8")
    policy = load_project(load_config(), root)
    assert path_profile(policy, target) == "docs"
    assert path_profile(policy, local) == "local"
    assert path_profile(policy, target, "local") == "local"
    diagnostic = project_mapping(policy, target)
    assert diagnostic["path_rule"] == "docs/**"
    assert diagnostic["check"]["llm.chatgpt.model"]["value"] == "gpt-6.1-sol"
    public = json.dumps(diagnostic, ensure_ascii=False)
    assert "Приватные инструкции" not in public and "Частный текст" not in public
    assert str(root) not in public
    forced = scan_paths([str(target)], load_config(), requested_profile="local")
    assert forced.files[0].profile == "local"


def test_init_validate_no_overwrite_and_targetless_commands(
    tmp_path: Path, isolated_appdata: Path, capsys: Any
) -> None:
    assert main(["init", str(tmp_path)]) == 0
    initial = (tmp_path / FILENAME).read_bytes()
    assert main(["init", str(tmp_path)]) == 2
    assert (tmp_path / FILENAME).read_bytes() == initial
    assert main(["config", "validate", str(tmp_path), "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["config", "show", str(tmp_path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["selected_profile"] == "everyday"
    assert main(["config", "show", "--environment", "--format", "json"]) == 0
    with pytest.raises(SystemExit) as version:
        main(["--version"])
    assert version.value.code == 0


def test_global_policy_is_ignored_but_environment_and_cli_remain(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = project(tmp_path / "repo")
    toml = tmp_path / "environment.toml"
    toml.write_text(
        """default_profile = "foreign"
[profiles.foreign.check]
engines = ["vale"]
[check]
engines = ["vale"]
terms = { "чужой" = "термин" }
[check.analysis]
mode = "deep"
[check.llm]
provider = "chatgpt"
[check.llm.chatgpt]
model = "gpt-6.1-sol"
effort = "high"
allow_cloud = true
[check.llm.llama_cpp]
endpoint = "http://127.0.0.1:4321"
""",
        encoding="utf-8",
    )
    policy = load_project(load_config(toml), root)
    settings, sources = policy.check_settings_for_profile(policy.default_profile)
    assert settings.llm_provider == "llama_cpp" and settings.analysis_mode == "normal"
    assert settings.terms == {} and settings.llm_chatgpt_model == "gpt-6-luna"
    assert settings.llm_chatgpt_allow_cloud
    assert settings.llm_llama_cpp_endpoint == "http://127.0.0.1:4321"
    assert sources["llm.provider"].startswith(FILENAME)
    overridden, _ = policy.check_settings_for_profile("everyday", {"analysis.mode": "deep"})
    assert overridden.analysis_mode == "deep"
    assert (
        overridden.analysis_max_requests is None
        and overridden.value("analysis.max_requests") == 100
    )


def test_clone_memory_scope_stable_and_distinct(tmp_path: Path, isolated_appdata: Path) -> None:
    first, second = project(tmp_path / "first"), project(tmp_path / "second")
    environment = load_config()
    assert (
        load_project(environment, first).project_id == load_project(environment, first).project_id
    )
    assert (
        load_project(environment, first).project_id != load_project(environment, second).project_id
    )
    before = (first / FILENAME).read_bytes()
    with pytest.raises(ConfigurationError):
        init_project(first)
    assert (first / FILENAME).read_bytes() == before


def test_real_git_worktree_has_own_policy_and_memory_scope(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = project(tmp_path / "repo")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", FILENAME], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Тест",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "test: исходная политика",
        ],
        check=True,
    )
    worktree = tmp_path / "отдельное дерево"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD"],
        check=True,
    )
    environment = load_config()
    assert (worktree / ".git").is_file()
    assert load_project(environment, worktree).project_root == worktree
    assert (
        load_project(environment, worktree).project_id != load_project(environment, root).project_id
    )


def test_windows_junction_cannot_supply_external_prompt(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    import os

    if os.name != "nt":
        pytest.skip("Проверка junction относится к Windows.")
    root = project(tmp_path / "repo", TEMPLATE + "prompts:\n  primary: escaped/prompt.md\n")
    external = tmp_path / "external"
    external.mkdir()
    (external / "prompt.md").write_text("Внешняя инструкция.", encoding="utf-8")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "escaped"), str(external)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ConfigurationError, match="внутри"):
        load_project(load_config(), root)


def test_all_policies_preflight_before_first_engine(tmp_path: Path, isolated_appdata: Path) -> None:
    from language_review.check import check_paths

    valid = project(tmp_path / "valid")
    (valid / "text.md").write_text("Русский текст.", encoding="utf-8")
    invalid = project(tmp_path / "invalid", TEMPLATE + "unknown: true\n")
    calls = []
    with pytest.raises(ConfigurationError, match="неизвестные"):
        check_paths(
            [str(valid), str(invalid)],
            load_config(),
            engine_factory=lambda *args: calls.append(args),
        )
    assert calls == []


def test_report_cannot_overwrite_policy_or_prompt(
    tmp_path: Path, isolated_appdata: Path, capsys: Any
) -> None:
    root = project(tmp_path / "repo", TEMPLATE + "prompts:\n  primary: policy.md\n")
    prompt = root / "policy.md"
    prompt.write_text("Частная инструкция.", encoding="utf-8")
    target = root / "text.md"
    target.write_text("Русский текст.", encoding="utf-8")
    for protected in (root / FILENAME, prompt):
        initial = protected.read_bytes()
        assert main(["extract", str(target), "--format", "json", "--output", str(protected)]) == 3
        assert protected.read_bytes() == initial
    assert "Частная инструкция" not in capsys.readouterr().out


def test_loaded_project_revalidates_changed_prompt_and_excludes_policy_files(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = project(tmp_path / "repo", TEMPLATE + "prompts:\n  primary: policy.md\n")
    prompt = root / "policy.md"
    prompt.write_text("Первая инструкция.", encoding="utf-8")
    policy = load_project(load_config(), root)
    prompt.write_text("Вторая инструкция.", encoding="utf-8")
    updated = load_project(policy, root)
    assert updated.profiles["everyday"].check_overrides["prompt_primary"] == "Вторая инструкция."
    assert scan_paths([str(root)], load_config()).selected_count == 0
    explicit = scan_paths([str(prompt)], load_config())
    assert explicit.files[0].reason == "project_configuration"


def test_check_junction_target_keeps_root_metadata_valid(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    import os

    from test_analysis import Provider

    from language_review.check import check_paths
    from language_review.engine_llm import LlmEngine

    if os.name != "nt":
        pytest.skip("Проверка junction относится к Windows.")
    root = project(tmp_path / "repo")
    (root / "text.md").write_text("Русский текст.", encoding="utf-8")
    alias = tmp_path / "alias"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(root)], check=True, capture_output=True
    )
    result = check_paths(
        [str(alias)],
        load_config(),
        check_overrides={"engines": ["llm"]},
        engine_factory=lambda name, settings: LlmEngine(settings, Provider()),
    )
    assert result.documents[0].extract.reason == "symbolic_link_not_followed"
    assert result.documents[0].configuration["policy"]["profile"] == "everyday"
