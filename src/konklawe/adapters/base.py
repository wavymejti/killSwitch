"""Provider adapter interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from konklawe.agents import AgentSpec
from konklawe.events import ParsedEvent


@dataclass
class CommandSpec:
    """How to start one turn: argv, optional text for stdin, environment additions."""

    argv: list[str]
    stdin_text: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class TurnRequest:
    agent: AgentSpec
    prompt: str
    workdir: Path
    resume_id: str | None  # provider_session_id from the previous turn


class TurnParser(ABC):
    """Stateful translator of one turn's stdout lines into common events."""

    @abstractmethod
    def feed(self, line: str) -> list[ParsedEvent]:
        """Translate one stdout line. Must never raise: unknown input becomes a `raw` event."""

    @abstractmethod
    def finish(self, exit_code: int | None, stderr_tail: list[str]) -> list[ParsedEvent]:
        """Events owed once the process has exited (e.g. an error when no result arrived)."""


class ProviderAdapter(ABC):
    """Translates between konklawe and one provider CLI."""

    name: ClassVar[str]

    @abstractmethod
    def validate_agent(self, spec: AgentSpec) -> list[str]:
        """Return provider-specific problems with `spec` (empty when valid)."""

    @abstractmethod
    def build_command(self, req: TurnRequest) -> CommandSpec: ...

    @abstractmethod
    def new_parser(self, agent: AgentSpec) -> TurnParser:
        """A fresh parser for one turn of `agent`."""
