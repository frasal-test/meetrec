"""Adapter interface and registry. The real adapters (anthropic, openai_compat)
are the only modules allowed to import a provider SDK, and they do it lazily (I9)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol

from ..keys import KeyStore
from ..types import LLMRequest, LLMResponse

if TYPE_CHECKING:
    from ...config import SummaryConfig


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


def default_registry(summary_config: SummaryConfig | None = None) -> AdapterRegistry:
    """The adapters shipped with the app. Importing them does not import any
    provider SDK (I9): each adapter loads its SDK on first use. The OCI region
    comes from the summary configuration; without one OCI reports not
    configured."""
    from .anthropic import AnthropicAdapter
    from .openai_compat import make_oci_adapter, make_openai_adapter

    oci_region = summary_config.oci_region if summary_config is not None else None
    registry = AdapterRegistry()
    registry.register("anthropic", AnthropicAdapter)
    registry.register("openai", make_openai_adapter)
    registry.register("oci", lambda store: make_oci_adapter(store, oci_region))
    return registry
