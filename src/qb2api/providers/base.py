"""Provider base class and registry."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..openai import ChatCompletionRequest


class Provider(ABC):
    """Abstract base class for API providers."""

    name: str

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest) -> dict:
        """Complete a chat request (non-streaming)."""
        ...

    @abstractmethod
    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        """Stream a chat request."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the provider and cleanup resources."""
        ...


class ProviderRegistry:
    """Registry for managing providers."""

    def __init__(self):
        self._providers: dict[str, Provider] = {}
        self._default_provider: str | None = None

    def clear(self) -> None:
        """Clear all providers. Called before re-initialization."""
        self._providers.clear()
        self._default_provider = None

    def register(self, provider: Provider, default: bool = False) -> None:
        """Register a provider."""
        self._providers[provider.name] = provider
        if default or not self._default_provider:
            self._default_provider = provider.name

    def get(self, name: str) -> Provider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    @property
    def providers(self) -> list[str]:
        """List registered provider names."""
        return list(self._providers.keys())

    async def close_all(self) -> None:
        """Close all providers and clear registry."""
        for provider in self._providers.values():
            await provider.close()
        self.clear()
