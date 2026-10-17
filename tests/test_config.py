from __future__ import annotations

import json
from pathlib import Path

from language_review.config import load_config
from language_review.errors import ConfigurationError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_precedence_profile_and_sources(tmp_path: Path, isolated_appdata: Path) -> None:
    user_path = isolated_appdata / "language-review" / "config.toml"
    user_path.parent.mkdir(parents=True)
    _write(
        user_path,
        """
[scan]
max_file_size = 10
exclude_patterns = ["user/**"]

[profiles.rust]
priority = 10
project_markers = ["Cargo.toml"]
match_paths = ["crates/**"]

[profiles.rust.scan]
extensions = [".rs"]
""",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "Cargo.toml").write_text("[package]", encoding="utf-8")
    explicit = _write(
        tmp_path / "explicit.toml",
        """
[scan]
max_file_size = 20

[profiles.rust]
priority = 20
""",
    )

    config = load_config(explicit)
    profile = config.profiles["rust"]
    mapping = config.as_mapping("rust")

    assert config.scan.max_file_size == 20
    assert config.sources["max_file_size"].endswith(str(explicit))
    assert profile.priority == 20
    assert profile.scan_overrides["extensions"] == (".rs",)
    assert mapping["scan"]["max_file_size"]["value"] == 20
    assert mapping["scan"]["max_file_size"]["source"].endswith(str(explicit))
    assert mapping["profiles"]["rust"]["priority"]["value"] == 20
    assert "Cargo.toml" in mapping["profiles"]["rust"]["project_markers"]["value"]


def test_invalid_unknown_values_and_missing_profile_fail(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    unknown = _write(tmp_path / "unknown.toml", "[scan]\nmax_file_szie = 5\n")
    malformed = _write(tmp_path / "malformed.toml", "[scan\n")
    missing_profile = _write(tmp_path / "profile.toml", 'default_profile = "missing"\n')

    for path in (unknown, malformed, missing_profile):
        try:
            load_config(path)
        except ConfigurationError:
            pass
        else:
            raise AssertionError(f"Ожидалась ошибка конфигурации: {path}")


def test_url_secrets_are_redacted_in_effective_configuration(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    config_path = _write(
        tmp_path / "secret.toml",
        """
[doctor.http_services]
local = "http://user:password@127.0.0.1:11434/path?token=secret"
""",
    )

    mapping = load_config(config_path).as_mapping()
    output = json.dumps(mapping, ensure_ascii=False)

    assert "password" not in output
    assert "token=secret" not in output
    assert "user:" not in output
    assert "***@" in output
    assert "query" not in output


def test_http_service_must_be_loopback(tmp_path: Path, isolated_appdata: Path) -> None:
    config_path = _write(
        tmp_path / "remote.toml",
        """
[doctor.http_services]
remote = "https://example.com/health"
""",
    )

    try:
        load_config(config_path)
    except ConfigurationError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("Внешний HTTP-сервис должен отклоняться")
