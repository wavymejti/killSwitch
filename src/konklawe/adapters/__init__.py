"""Registry of provider adapters."""

from konklawe.adapters.base import ProviderAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {}


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
