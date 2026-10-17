"""Adapter interface and registry. Real adapters (the only modules allowed to
import a provider SDK, I9) are added later."""
from __future__ import annotations

from typing import Callable, Protocol

from ..keys import KeyStore
from ..types import LLMRequest, LLMResponse


class ProviderAdapter(Protocol):
    provider: str

    def is_configured(self) -> bool:
        """Only checks that a key exists. No SDK import, no network."""

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Raises only LLMError."""


AdapterFactory = Callable[[KeyStore], ProviderAdapter]


class AdapterRegistry:
    """provider -> factory building the adapter from a KeyStore."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, provider: str, factory: AdapterFactory) -> None:
        self._factories[provider] = factory

    def has(self, provider: str) -> bool:
        return provider in self._factories

    def providers(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def create(self, provider: str, keystore: KeyStore) -> ProviderAdapter | None:
        factory = self._factories.get(provider)
        return factory(keystore) if factory else None
