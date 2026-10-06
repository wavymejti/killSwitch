"""Agent definitions: Markdown files with a YAML header in the agents directory."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

if TYPE_CHECKING:
    from konklawe.adapters.base import ProviderAdapter

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
HEADER_DELIMITER = "---"
# Set by the loader, never read from the header.
LOADER_FIELDS = frozenset({"role_prompt", "source_path"})

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
    description: NonEmptyStr
    provider: NonEmptyStr
    model: str | None = None
    tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    permission_mode: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    role_prompt: NonEmptyStr
    source_path: Path

    @field_validator("tools", "disallowed_tools", mode="before")
    @classmethod
    def _split_tool_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return split_tool_list(value)
        if isinstance(value, list):
            return [item.strip() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("tools", "disallowed_tools")
    @classmethod
    def _no_empty_tools(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("tool names must not be empty")
        return value

    @field_validator("provider_options", mode="before")
    @classmethod
    def _none_is_empty_options(cls, value: Any) -> Any:
        return {} if value is None else value


def split_tool_list(text: str) -> list[str]:
    """Split "Read, Bash(git diff:*, git log:*)" on commas outside parentheses."""
    items: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        current.append(char)
    items.append("".join(current).strip())
    return [item for item in items if item]


class AgentLoadError(Exception):
    """One or more agent files are invalid; `errors` holds (path, message) pairs."""

    def __init__(self, errors: list[tuple[Path, str]]) -> None:
        self.errors = errors
        super().__init__("\n".join(f"{path}: {message}" for path, message in errors))


@dataclass
class AgentScan:
    """Result of reading an agents directory: valid agents plus per-file errors."""

    agents: dict[str, AgentSpec] = field(default_factory=dict)
    errors: list[tuple[Path, str]] = field(default_factory=list)


def load_agent(path: Path, adapters: Mapping[str, "ProviderAdapter"] | None = None) -> AgentSpec:
    """Load and validate one agent file. Raises `AgentLoadError` listing every problem."""
    spec, problems = _load_agent(path, _resolve_adapters(adapters))
    if problems or spec is None:
        raise AgentLoadError([(path, message) for message in problems])
    return spec


def scan_agents(
    directory: Path, adapters: Mapping[str, "ProviderAdapter"] | None = None
) -> AgentScan:
    """Load every `*.md` file in `directory`, collecting errors instead of stopping at the first."""
    scan = AgentScan()
    if not directory.is_dir():
        scan.errors.append((directory, "agents directory does not exist"))
        return scan
    registry = _resolve_adapters(adapters)
    for path in sorted(directory.glob("*.md")):
        spec, problems = _load_agent(path, registry)
        scan.errors.extend((path, message) for message in problems)
        if spec is None or problems:
            continue
        if spec.name in scan.agents:
            other = scan.agents[spec.name].source_path
            scan.errors.append((path, f"duplicate agent name '{spec.name}' (also in {other})"))
            continue
        scan.agents[spec.name] = spec
    return scan


def load_agents(
    directory: Path, adapters: Mapping[str, "ProviderAdapter"] | None = None
) -> dict[str, AgentSpec]:
    """Load all agents from `directory`. Raises `AgentLoadError` with errors from all files."""
    scan = scan_agents(directory, adapters)
    if scan.errors:
        raise AgentLoadError(scan.errors)
    return scan.agents


def _resolve_adapters(
    adapters: Mapping[str, "ProviderAdapter"] | None,
) -> Mapping[str, "ProviderAdapter"]:
    if adapters is not None:
        return adapters
    from konklawe.adapters import adapters as registered

    return registered()


def _load_agent(
    path: Path, adapters: Mapping[str, "ProviderAdapter"]
) -> tuple[AgentSpec | None, list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"cannot read file: {exc}"]

    parsed = _split_header(text)
    if isinstance(parsed, str):
        return None, [parsed]
    header, body = parsed

    reserved = sorted(LOADER_FIELDS & header.keys())
    if reserved:
        return None, [f"unknown field '{key}'" for key in reserved]

    try:
        spec = AgentSpec.model_validate({**header, "role_prompt": body, "source_path": path})
    except ValidationError as exc:
        return None, _format_validation_errors(exc)

    problems: list[str] = []
    if spec.name != path.stem:
        problems.append(f"name '{spec.name}' does not match file name '{path.stem}'")
    adapter = adapters.get(spec.provider)
    if adapter is None:
        available = ", ".join(sorted(adapters)) or "none"
        problems.append(f"unknown provider '{spec.provider}' (available: {available})")
    else:
        problems.extend(adapter.validate_agent(spec))
    return spec, problems


def _split_header(text: str) -> tuple[dict[str, Any], str] | str:
    """Split a file into its YAML header and body, or return an error message."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != HEADER_DELIMITER:
        return f"file must start with a '{HEADER_DELIMITER}' line (YAML header)"
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == HEADER_DELIMITER:
            header_text = "".join(lines[1:idx])
            body = "".join(lines[idx + 1 :])
            break
    else:
        return f"YAML header is not closed with a '{HEADER_DELIMITER}' line"

    try:
        header = yaml.safe_load(header_text)
    except yaml.YAMLError as exc:
        return f"invalid YAML header: {exc}"
    if header is None:
        header = {}
    if not isinstance(header, dict):
        return "YAML header must be a mapping of fields"
    return {str(key): value for key, value in header.items()}, body


def _format_validation_errors(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        key = ".".join(str(part) for part in err["loc"])
        if err["type"] == "extra_forbidden":
            messages.append(f"unknown field '{key}'")
        elif err["type"] == "missing":
            messages.append(f"missing required field '{key}'")
        elif key == "role_prompt":
            messages.append("role prompt (text below the header) must not be empty")
        else:
            messages.append(f"invalid field '{key}': {err['msg']}")
    return messages
