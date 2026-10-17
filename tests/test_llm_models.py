from __future__ import annotations

import dataclasses
import unittest
from decimal import Decimal

from meeting_recorder.llm.errors import (
    AuthError,
    BadRequestError,
    ContentFilteredError,
    InsufficientCreditsError,
    InvalidResponseError,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    TimeoutLLMError,
    UnknownLLMError,
)
from meeting_recorder.llm.keys import InMemoryKeyStore
from meeting_recorder.llm.models import (
    MAX_INPUT_TOKENS,
    MODEL_TABLE,
    compute_cost,
    estimate_max_cost,
    estimate_tokens,
    find_model,
    fits,
)
from meeting_recorder.llm.types import Usage


class ModelTableTests(unittest.TestCase):
    def test_table_has_one_model_per_provider_in_order(self):
        self.assertEqual(
            [(m.provider, m.model_id) for m in MODEL_TABLE],
            [
                ("anthropic", "claude-haiku-5-5"),
                ("openai", "gpt-6-luna"),
                ("oci", "openai.gpt-oss-120b"),
            ],
        )
        self.assertEqual(MAX_INPUT_TOKENS, 100_000)
        for spec in MODEL_TABLE:
            self.assertEqual(spec.currency, "USD")
            self.assertEqual(spec.max_output_tokens, 16000)

    def test_prices_are_decimals(self):
        haiku = find_model("claude-haiku-5-5")
        self.assertEqual(
            (haiku.price_input, haiku.price_output),
            (Decimal("0.10"), Decimal("0.50")),
        )
        oci = find_model("openai.gpt-oss-120b")
        self.assertEqual(
            (oci.price_input, oci.price_output),
            (Decimal("0.15"), Decimal("0.60")),
        )
        self.assertIsNone(find_model("nope"))

    def test_estimate_tokens_is_ceil_of_chars_over_four(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)

    def test_T21_model_missing_from_the_table_has_no_cost_not_zero(self):
        cost = compute_cost("not-in-the-table", Usage(1000, 500))
        self.assertIsNone(cost.amount)
        self.assertEqual(cost.currency, "USD")

    def test_T21_missing_tokens_have_no_cost_not_zero(self):
        for usage in (Usage(None, 5), Usage(5, None), Usage(None, None)):
            self.assertIsNone(compute_cost("claude-haiku-5-5", usage).amount)

    def test_compute_cost_formula(self):
        cost = compute_cost("openai.gpt-oss-120b", Usage(2_000_000, 1_000_000))
        self.assertEqual(cost.amount, Decimal("0.90"))
        self.assertEqual(
            compute_cost("claude-haiku-5-5", Usage(0, 0)).amount, Decimal(0)
        )

    def test_fits_applies_both_i17_conditions(self):
        haiku = find_model("claude-haiku-5-5")
        self.assertTrue(fits(haiku, MAX_INPUT_TOKENS))
        self.assertFalse(fits(haiku, MAX_INPUT_TOKENS + 1))
        tight = dataclasses.replace(haiku, context_window=20_000)
        self.assertTrue(fits(tight, 4_000))
        self.assertFalse(fits(tight, 4_001))

    def test_estimate_max_cost_caps_output_at_max_output_tokens(self):
        haiku = find_model("claude-haiku-5-5")
        self.assertEqual(estimate_max_cost(haiku, 0), Decimal(0))
        self.assertEqual(estimate_max_cost(haiku, 16_000), Decimal("0.0096"))


class ErrorTests(unittest.TestCase):
    def test_retryable_flags_follow_the_contract(self):
        retryable = (TimeoutLLMError, RateLimitError, ProviderError)
        not_retryable = (
            InsufficientCreditsError,
            AuthError,
            ModelNotFoundError,
            BadRequestError,
            ContentFilteredError,
            InvalidResponseError,
            UnknownLLMError,
        )
        for cls in retryable:
            self.assertTrue(cls.retryable, cls.__name__)
        for cls in not_retryable:
            self.assertFalse(cls.retryable, cls.__name__)
        self.assertFalse(LLMError.retryable)

    def test_errors_carry_usage_cost_and_a_user_message(self):
        error = TimeoutLLMError(usage=Usage(1, 2))
        self.assertEqual(error.usage, Usage(1, 2))
        self.assertIsNone(error.cost)
        self.assertEqual(str(error), error.user_message)
        for cls in (
            TimeoutLLMError, RateLimitError, ProviderError,
            InsufficientCreditsError, AuthError, ModelNotFoundError,
            BadRequestError, ContentFilteredError, InvalidResponseError,
            UnknownLLMError,
        ):
            self.assertTrue(cls().user_message)
            self.assertIsInstance(cls(), LLMError)


class KeyStoreTests(unittest.TestCase):
    def test_repr_never_shows_the_keys(self):
        store = InMemoryKeyStore({"anthropic": "sk-FAKE-KEY-DO-NOT-LEAK-123"})
        self.assertTrue(store.has_key("anthropic"))
        self.assertFalse(store.has_key("openai"))
        self.assertEqual(store.get_key("anthropic"), "sk-FAKE-KEY-DO-NOT-LEAK-123")
        self.assertIsNone(store.get_key("openai"))
        self.assertNotIn("sk-FAKE", repr(store))
        self.assertNotIn("sk-FAKE", str(store))
        self.assertIn("anthropic", repr(store))


if __name__ == "__main__":
    unittest.main()
