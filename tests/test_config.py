from __future__ import annotations

import json
from pathlib import Path

import pytest

from language_review.config import CheckSettings, load_config
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


def test_profile_message_call_sources_include_all_contributors(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    user_path = isolated_appdata / "language-review" / "config.toml"
    user_path.parent.mkdir(parents=True)
    _write(
        user_path,
        """
[extract]
message_calls = ["UserBaseLogger"]

[profiles.logging.extract]
message_calls = ["UserProfileLogger"]
""",
    )
    explicit = _write(
        tmp_path / "explicit.toml",
        """
[extract]
message_calls = ["ExplicitBaseLogger"]

[profiles.logging.extract]
message_calls = ["ExplicitProfileLogger"]
""",
    )

    mapping = load_config(explicit).as_mapping("logging")

    effective = mapping["extract"]["message_calls"]
    profile = mapping["profiles"]["logging"]["extract"]["message_calls"]
    assert effective["value"] == [
        "ExplicitBaseLogger",
        "UserProfileLogger",
        "ExplicitProfileLogger",
    ]
    assert str(explicit) in effective["source"]
    assert str(user_path) in effective["source"]
    assert profile["value"] == ["UserProfileLogger", "ExplicitProfileLogger"]
    assert str(user_path) in profile["source"]
    assert str(explicit) in profile["source"]


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


def test_invalid_selector_message_has_no_trailing_whitespace(tmp_path: Path) -> None:
    invalid = _write(
        tmp_path / "invalid-selector.toml",
        '[extract.selectors]\n".json" = ["ui.message"]\n',
    )

    with pytest.raises(ConfigurationError) as error:
        load_config(invalid)

    assert str(error.value).endswith("должен начинаться с символа $.")
    assert not str(error.value).endswith(" ")


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


def test_check_settings_defaults_precedence_profiles_and_cli(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    user_path = isolated_appdata / "language-review" / "config.toml"
    user_path.parent.mkdir(parents=True)
    _write(
        user_path,
        """
[check]
terms = { "юзер" = "пользователь" }
[check.llm.llama_cpp]
endpoint = "http://127.0.0.1:18080/?api_key=secret"
""",
    )
    explicit = _write(
        tmp_path / "explicit.toml",
        """
[check]
engines = ["languagetool", "vale"]
terms = { "фича" = "функция" }
[check.vale]
config = "styles/.vale.ini"

[profiles.docs.check]
engines = ["vale"]
categories = ["terminology"]
terms = { "апдейт" = "обновление" }
""",
    )

    defaults = CheckSettings()
    config = load_config(explicit)
    base, base_sources = config.check_settings_for_profile(None)
    profile, profile_sources = config.check_settings_for_profile(
        "docs", {"llm.provider": "ollama", "engines": None}
    )
    mapping = config.as_mapping("docs")

    assert defaults.engines == ("languagetool", "llm", "vale")
    assert defaults.llm_provider == "llama_cpp"
    assert defaults.llm_llama_cpp_endpoint == "http://127.0.0.1:8080"
    assert defaults.languagetool_language == "ru-RU"
    assert base.engines == ("languagetool", "vale")
    assert base.terms == {"юзер": "пользователь", "фича": "функция"}
    assert base.vale_config == str((tmp_path / "styles" / ".vale.ini").resolve())
    assert str(user_path) in base_sources["terms"] and str(explicit) in base_sources["terms"]
    assert profile.engines == ("vale",)
    assert profile.categories == ("terminology",)
    assert profile.terms == {"юзер": "пользователь", "фича": "функция", "апдейт": "обновление"}
    assert profile.llm_provider == "ollama"
    assert profile_sources["llm.provider"] == "аргумент командной строки"
    assert profile_sources["engines"].startswith("профиль docs из")
    assert mapping["check"]["engines"]["value"] == ["vale"]
    assert mapping["profiles"]["docs"]["check"]["categories"]["value"] == ["terminology"]
    assert "secret" not in json.dumps(mapping, ensure_ascii=False)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[check]\nengines = ["spellcheck"]\n', "неизвестные значения"),
        ("[check]\nengines = []\n", "хотя бы один движок"),
        ('[check]\ncategories = ["poetry"]\n', "неизвестные значения"),
        ('[check.llm]\nprovider = "openai"\n', "llama_cpp, ollama"),
        ("[check.llm]\nbatch_items = 0\n", "от 1 до 64"),
        ("[check.languagetool]\ntimeout_seconds = 0\n", "от 0 до 3600"),
        ('[check.languagetool]\nendpoint = "ftp://127.0.0.1"\n', "HTTP или HTTPS"),
        ('[check.languagetool]\ndisabled_rules = ["BAD RULE"]\n', "идентификатор правила"),
        ("[check]\nunknown = 1\n", "Неизвестный параметр"),
        ('[check.vale]\nmodel = "x"\n', "Неизвестный параметр"),
        ('[check]\nterms = { "юзер" = "" }\n', "непустые строки"),
        ('[profiles.docs.check.languagetool]\nlanguage = "en"\n', "только engines"),
        ('[check.llm.ollama]\nendpoint = "http://10.0.0.5:11434"\n', "allow_remote_endpoints"),
    ],
)
def test_invalid_check_settings_fail(
    tmp_path: Path, isolated_appdata: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config(_write(tmp_path / "bad.toml", text))


def test_remote_endpoint_check_runs_after_merging_all_files(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    user_path = isolated_appdata / "language-review" / "config.toml"
    user_path.parent.mkdir(parents=True)
    _write(user_path, '[check.llm.ollama]\nendpoint = "http://10.0.0.5:11434/?token=secret"\n')
    allow = _write(tmp_path / "allow.toml", "[check]\nallow_remote_endpoints = true\n")

    config = load_config(allow)
    settings, sources = config.check_settings_for_profile(None)

    assert settings.llm_ollama_endpoint.startswith("http://10.0.0.5:11434")
    assert str(allow) in sources["allow_remote_endpoints"]
    assert "secret" not in json.dumps(config.as_mapping(), ensure_ascii=False)


def test_explicit_config_can_forbid_remote_endpoints_allowed_by_user_config(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    user_path = isolated_appdata / "language-review" / "config.toml"
    user_path.parent.mkdir(parents=True)
    _write(
        user_path,
        "[check]\nallow_remote_endpoints = true\n"
        '[check.llm.ollama]\nendpoint = "http://10.0.0.5:11434"\n',
    )
    deny = _write(tmp_path / "deny.toml", "[check]\nallow_remote_endpoints = false\n")

    load_config()
    with pytest.raises(ConfigurationError, match="allow_remote_endpoints"):
        load_config(deny)
