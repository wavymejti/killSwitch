"""Application settings, loaded from an optional `konklawe.toml`."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_FILENAME = "konklawe.toml"

DEFAULT_STRIP_ENV: tuple[str, ...] = (
    # Would switch Claude Code from subscription to API billing.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    # Inherited when konklawe itself runs inside a Claude Code session; the child
    # would otherwise treat itself as a sub-session of that parent.
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "AI_AGENT",
)


class ConfigError(Exception):
    """Invalid or unreadable configuration."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agents_dir: Path = Path("agents")
    data_dir: Path = Path(".konklawe")
    max_parallel_turns: int = Field(default=3, ge=1)
    turn_timeout_s: float = Field(default=1800, gt=0)
    kill_grace_s: float = Field(default=5, ge=0)
    strip_env: list[str] = Field(default_factory=lambda: list(DEFAULT_STRIP_ENV))
    store_raw_lines: bool = True
    stdout_line_limit: int = Field(default=16 * 1024 * 1024, ge=64 * 1024)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "konklawe.db"


def load_settings(directory: Path | None = None) -> Settings:
    """Load settings from `konklawe.toml` in `directory` (default: the current directory).

    Without the file, defaults are used. Relative paths are resolved against `directory`,
    which is also the directory holding the file.
    """
    base = (directory or Path.cwd()).resolve()
    path = base / CONFIG_FILENAME
    raw: dict[str, object] = {}
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"{path}: cannot read config: {exc}") from exc

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc

    return settings.model_copy(
        update={
            "agents_dir": _resolve(base, settings.agents_dir),
            "data_dir": _resolve(base, settings.data_dir),
        }
    )


def _resolve(base: Path, path: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else base / path


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        key = ".".join(str(part) for part in err["loc"]) or "<root>"
        if err["type"] == "extra_forbidden":
            lines.append(f"{path}: unknown key '{key}'")
        else:
            lines.append(f"{path}: invalid value for '{key}': {err['msg']}")
    return "\n".join(lines)
