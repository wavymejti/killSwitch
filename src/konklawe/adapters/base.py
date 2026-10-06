"""Provider adapter interface."""

from abc import ABC, abstractmethod
from typing import ClassVar

from konklawe.agents import AgentSpec


class ProviderAdapter(ABC):
    """Translates between konklawe and one provider CLI."""

    name: ClassVar[str]

    @abstractmethod
    def validate_agent(self, spec: AgentSpec) -> list[str]:
        """Return provider-specific problems with `spec` (empty when valid)."""
