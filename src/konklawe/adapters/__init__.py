"""Registry of provider adapters."""

from konklawe.adapters.base import ProviderAdapter
from konklawe.adapters.claude import ClaudeAdapter
from konklawe.adapters.fake import FakeAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    adapter.name: adapter for adapter in (ClaudeAdapter(), FakeAdapter())
}


class UnknownProviderError(LookupError):
    pass


def adapters() -> dict[str, ProviderAdapter]:
    """All registered adapters by provider name."""
    return dict(_ADAPTERS)


def get_adapter(name: str) -> ProviderAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise UnknownProviderError(
            f"unknown provider '{name}' (available: {', '.join(available_providers()) or 'none'})"
        ) from None


def available_providers() -> list[str]:
    return sorted(_ADAPTERS)
