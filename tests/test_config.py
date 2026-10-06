from pathlib import Path

import pytest

from konklawe.config import CONFIG_FILENAME, ConfigError, Settings, load_settings


def write_config(directory: Path, text: str) -> None:
    (directory / CONFIG_FILENAME).write_text(text, encoding="utf-8")


def test_defaults_without_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    assert settings.agents_dir == tmp_path / "agents"
    assert settings.data_dir == tmp_path / ".konklawe"
    assert settings.db_path == tmp_path / ".konklawe" / "konklawe.db"
    assert settings.max_parallel_turns == 3
    assert settings.turn_timeout_s == 1800
    assert settings.kill_grace_s == 5
    assert {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE"} <= set(settings.strip_env)
    assert settings.store_raw_lines is True
    assert settings.stdout_line_limit == 16 * 1024 * 1024


def test_defaults_use_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert load_settings().agents_dir == tmp_path.resolve() / "agents"


def test_override_from_file(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        """
agents_dir = "/opt/agents"
data_dir = "var/data"
max_parallel_turns = 5
turn_timeout_s = 60.5
strip_env = ["FOO_KEY"]
store_raw_lines = false
""",
    )

    settings = load_settings(tmp_path)

    assert settings.agents_dir == Path("/opt/agents")
    assert settings.data_dir == tmp_path / "var" / "data"
    assert settings.max_parallel_turns == 5
    assert settings.turn_timeout_s == 60.5
    assert settings.strip_env == ["FOO_KEY"]
    assert settings.store_raw_lines is False
    assert settings.kill_grace_s == 5


def test_unknown_key_is_reported_by_name(tmp_path: Path) -> None:
    write_config(tmp_path, "max_paralel_turns = 2\n")

    with pytest.raises(ConfigError, match="unknown key 'max_paralel_turns'"):
        load_settings(tmp_path)


def test_invalid_value(tmp_path: Path) -> None:
    write_config(tmp_path, "max_parallel_turns = 0\n")

    with pytest.raises(ConfigError, match="invalid value for 'max_parallel_turns'"):
        load_settings(tmp_path)


def test_malformed_toml(tmp_path: Path) -> None:
    write_config(tmp_path, "max_parallel_turns = \n")

    with pytest.raises(ConfigError, match="cannot read config"):
        load_settings(tmp_path)


def test_settings_are_immutable() -> None:
    settings = Settings()

    with pytest.raises(ValueError):
        settings.max_parallel_turns = 10  # type: ignore[misc]
