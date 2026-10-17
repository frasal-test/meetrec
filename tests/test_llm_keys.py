"""The on-demand key store: a key is fetched only when an adapter asks for it."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_recorder.llm.keys import OnDemandKeyStore  # noqa: E402

FAKE_KEY = "sk-test-NEVER-SHOW-0123456789"


class OnDemandKeyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetched: list[str] = []

    def store(self, *providers: str, answer: str | None = FAKE_KEY) -> OnDemandKeyStore:
        def fetch(provider: str) -> str | None:
            self.fetched.append(provider)
            return answer

        return OnDemandKeyStore(frozenset(providers), fetch)

    def test_has_key_never_fetches(self) -> None:
        store = self.store("anthropic")
        self.assertTrue(store.has_key("anthropic"))
        self.assertFalse(store.has_key("openai"))
        self.assertEqual(self.fetched, [])

    def test_a_key_is_fetched_once_and_only_for_that_provider(self) -> None:
        store = self.store("anthropic", "openai", "oci")
        self.assertEqual(store.get_key("anthropic"), FAKE_KEY)
        self.assertEqual(store.get_key("anthropic"), FAKE_KEY)
        self.assertEqual(self.fetched, ["anthropic"])

    def test_a_provider_without_a_key_is_never_fetched(self) -> None:
        store = self.store("anthropic")
        self.assertIsNone(store.get_key("openai"))
        self.assertEqual(self.fetched, [])

    def test_an_unreadable_key_is_none_and_not_asked_again(self) -> None:
        for answer in (None, "", "   "):
            with self.subTest(answer=answer):
                self.fetched.clear()
                store = self.store("anthropic", answer=answer)
                self.assertIsNone(store.get_key("anthropic"))
                self.assertIsNone(store.get_key("anthropic"))
                self.assertEqual(self.fetched, ["anthropic"])

    def test_the_key_is_stripped(self) -> None:
        store = self.store("anthropic", answer=f"  {FAKE_KEY}\n")
        self.assertEqual(store.get_key("anthropic"), FAKE_KEY)

    def test_repr_never_shows_a_key(self) -> None:
        store = self.store("anthropic")
        store.get_key("anthropic")
        self.assertNotIn(FAKE_KEY, repr(store))
        self.assertNotIn(FAKE_KEY, str(store))


if __name__ == "__main__":
    unittest.main()
