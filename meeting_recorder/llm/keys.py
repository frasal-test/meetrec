"""Read-only view of the API keys.

In production the Swift app reads the macOS Keychain and hands the keys to the
Python process on stdin, once, at start. Python never reads the Keychain and
never puts a key in argv, the environment or a log.
"""
from __future__ import annotations

from typing import Protocol


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
