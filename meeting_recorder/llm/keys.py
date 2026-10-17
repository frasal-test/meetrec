"""Read-only view of the API keys.

In production the Swift app tells the Python process, at start, which
providers have a key (without reading any secret), and hands over a key on
stdin only when an adapter needs it for a call. Python never reads the
Keychain and never puts a key in argv, the environment or a log.
"""
from __future__ import annotations

from typing import Callable, Protocol


class KeyStore(Protocol):
    def has_key(self, provider: str) -> bool: ...

    def get_key(self, provider: str) -> str | None: ...


class InMemoryKeyStore(dict):
    """provider -> key, kept in memory only. repr never shows the keys."""

    def has_key(self, provider: str) -> bool:
        return bool(self.get(provider))

    def get_key(self, provider: str) -> str | None:
        return self.get(provider) or None

    def __repr__(self) -> str:
        return f"InMemoryKeyStore(providers={sorted(self)!r})"

    __str__ = __repr__


class OnDemandKeyStore:
    """Knows which providers have a key; fetches a key only when asked for
    it, at most once per provider (a Retry reuses it).

    In the app each fetch reads one Keychain item, which may make macOS ask
    for the password: so only the provider actually called is fetched.
    `fetch` returns None when the key could not be read.
    """

    def __init__(
        self,
        providers: frozenset[str],
        fetch: Callable[[str], str | None],
    ) -> None:
        self._providers = providers
        self._fetch = fetch
        self._keys: dict[str, str | None] = {}

    def has_key(self, provider: str) -> bool:
        return provider in self._providers

    def get_key(self, provider: str) -> str | None:
        if provider not in self._providers:
            return None
        if provider not in self._keys:
            key = self._fetch(provider)
            self._keys[provider] = key.strip() if key and key.strip() else None
        return self._keys[provider]

    def __repr__(self) -> str:
        return f"OnDemandKeyStore(providers={sorted(self._providers)!r})"

    __str__ = __repr__
