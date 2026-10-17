"""Anthropic adapter against a fake `anthropic` module: no network, no real
SDK, no real key (CLAUDE.md)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_support import (  # noqa: E402
    CHOOSE_ANTHROPIC_IT,
    FAKE_KEY,
    Harness,
)

from meeting_recorder.llm import errors as E  # noqa: E402
from meeting_recorder.llm.adapters import default_registry  # noqa: E402
from meeting_recorder.llm.adapters.anthropic import AnthropicAdapter  # noqa: E402
from meeting_recorder.llm.checks import startup_check  # noqa: E402
from meeting_recorder.llm.keys import InMemoryKeyStore  # noqa: E402
from meeting_recorder.llm.models import compute_cost  # noqa: E402
from meeting_recorder.llm.outcomes import OutcomeKind  # noqa: E402
from meeting_recorder.llm.types import LLMRequest, LLMResponse, Usage  # noqa: E402


# --- the fake SDK ---------------------------------------------------------


class APIError(Exception):
    pass


class APIConnectionError(APIError):
    def __init__(self, message: str = "Connection error.") -> None:
        super().__init__(message)
        self.message = message


class APITimeoutError(APIConnectionError):
    pass


class APIStatusError(APIError):
    status_code_default = None

    def __init__(self, message: str, *, status_code=None, body=None, type=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code or self.status_code_default
        self.body = body
        self.type = type


def _status_class(name: str, status: int):
    return type(name, (APIStatusError,), {"status_code_default": status})


BadRequestError = _status_class("BadRequestError", 400)
AuthenticationError = _status_class("AuthenticationError", 401)
PermissionDeniedError = _status_class("PermissionDeniedError", 403)
NotFoundError = _status_class("NotFoundError", 404)
UnprocessableEntityError = _status_class("UnprocessableEntityError", 422)
RateLimitError = _status_class("RateLimitError", 429)
InternalServerError = _status_class("InternalServerError", 500)


class FakeTimeout:
    def __init__(self, timeout=None, *, connect=None, read=None, write=None, pool=None):
        self.timeout = timeout
        self.connect = connect


class FakeAnthropicClient:
    """Captures the constructor kwargs; `messages.create` and
    `models.retrieve` return or raise the next scripted item."""

    def __init__(self, sdk, **kwargs):
        self.kwargs = kwargs
        self.sdk = sdk
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    def _next(self, queue):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def _create(self, **kwargs):
        self.sdk.create_calls.append(kwargs)
        return self._next(self.sdk.create_script)

    def _retrieve(self, *args, **kwargs):
        self.sdk.retrieve_calls.append((args, kwargs))
        return self._next(self.sdk.retrieve_script)


def make_fake_sdk(create=None, retrieve=None) -> types.ModuleType:
    sdk = types.ModuleType("anthropic")
    sdk.create_script = list(create or [])
    sdk.retrieve_script = list(retrieve if retrieve is not None else [SimpleNamespace()])
    sdk.create_calls = []
    sdk.retrieve_calls = []
    sdk.clients = []

    def anthropic_factory(**kwargs):
        client = FakeAnthropicClient(sdk, **kwargs)
        sdk.clients.append(client)
        return client

    sdk.Anthropic = anthropic_factory
    sdk.Timeout = FakeTimeout
    for cls in (
        APIError,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        BadRequestError,
        AuthenticationError,
        PermissionDeniedError,
        NotFoundError,
        UnprocessableEntityError,
        RateLimitError,
        InternalServerError,
    ):
        setattr(sdk, cls.__name__, cls)
    return sdk


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def thinking_block():
    return SimpleNamespace(type="thinking", thinking="", text="")


def message(content=None, stop_reason="end_turn", input_tokens=1000, output_tokens=500):
    return SimpleNamespace(
        content=[text_block("## Sintesi\nOk.")] if content is None else content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def request(**overrides) -> LLMRequest:
    values = dict(
        run_id="run-1",
        provider="anthropic",
        model_id="claude-haiku-5-5",
        system_prompt="SYSTEM PROMPT",
        transcript="<transcript>testo</transcript>",
        template_id="default",
        template_language="it",
        response_language="it",
        max_output_tokens=16000,
    )
    values.update(overrides)
    return LLMRequest(**values)


class AdapterTestCase(unittest.TestCase):
    def adapter(self, create=None, retrieve=None, key=FAKE_KEY):
        self.sdk = make_fake_sdk(create, retrieve)
        store = InMemoryKeyStore()
        if key:
            store["anthropic"] = key
        return AnthropicAdapter(store, sdk=self.sdk)

    def call_error(self, exc, **kwargs) -> E.LLMError:
        adapter = self.adapter([exc], **kwargs)
        with self.assertRaises(E.LLMError) as caught:
            adapter.complete(request())
        return caught.exception


def credit_balance_error(cls=BadRequestError, **kwargs):
    text = (
        "Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits."
    )
    return cls(
        text,
        body={"type": "error", "error": {"type": "invalid_request_error", "message": text}},
        type="invalid_request_error",
        **kwargs,
    )


# --- errors ---------------------------------------------------------------


class ErrorMappingTests(AdapterTestCase):
    def test_T28_credit_balance_400_is_insufficient_credits(self):
        error = self.call_error(credit_balance_error())
        self.assertIsInstance(error, E.InsufficientCreditsError)
        self.assertFalse(error.retryable)
        self.assertIsNone(error.usage)
        self.assertIsNone(error.cost)

    def test_T28_credit_balance_in_the_body_only_is_insufficient_credits(self):
        exc = BadRequestError(
            "Error code: 400",
            body={"error": {"type": "invalid_request_error", "message": "Credit Balance is too low"}},
            type="invalid_request_error",
        )
        self.assertIsInstance(self.call_error(exc), E.InsufficientCreditsError)

    def test_T28_402_billing_error_is_insufficient_credits(self):
        exc = APIStatusError("Payment required", status_code=402, type="billing_error")
        error = self.call_error(exc)
        self.assertIsInstance(error, E.InsufficientCreditsError)
        self.assertFalse(error.retryable)

    def test_T28_billing_error_type_wins_over_status(self):
        exc = APIStatusError(
            "x", status_code=400, body={"error": {"type": "billing_error", "message": "x"}}
        )
        self.assertIsInstance(self.call_error(exc), E.InsufficientCreditsError)

    def test_timeout_is_timeout_error_and_retryable(self):
        error = self.call_error(APITimeoutError("Request timed out."))
        self.assertIsInstance(error, E.TimeoutLLMError)
        self.assertTrue(error.retryable)

    def test_connection_error_is_unknown_error(self):
        # DA DEFINIRE: the contract has no class for offline/DNS/refused.
        error = self.call_error(APIConnectionError("Connection error."))
        self.assertIsInstance(error, E.UnknownLLMError)
        self.assertFalse(error.retryable)

    def test_other_400_is_bad_request(self):
        exc = BadRequestError(
            "max_tokens: too large",
            body={"error": {"type": "invalid_request_error", "message": "max_tokens: too large"}},
            type="invalid_request_error",
        )
        error = self.call_error(exc)
        self.assertIs(type(error), E.BadRequestError)
        self.assertFalse(error.retryable)

    def test_413_and_422_are_bad_request(self):
        for exc in (
            APIStatusError("too big", status_code=413, type="request_too_large"),
            UnprocessableEntityError("bad", type="invalid_request_error"),
        ):
            with self.subTest(status=exc.status_code):
                self.assertIs(type(self.call_error(exc)), E.BadRequestError)

    def test_401_and_403_are_auth_error(self):
        for exc in (
            AuthenticationError("bad key", type="authentication_error"),
            PermissionDeniedError("nope", type="permission_error"),
        ):
            with self.subTest(status=exc.status_code):
                error = self.call_error(exc)
                self.assertIs(type(error), E.AuthError)
                self.assertFalse(error.retryable)

    def test_404_is_model_not_found(self):
        error = self.call_error(NotFoundError("model", type="not_found_error"))
        self.assertIs(type(error), E.ModelNotFoundError)
        self.assertFalse(error.retryable)

    def test_429_is_rate_limit_and_retryable(self):
        error = self.call_error(RateLimitError("slow down", type="rate_limit_error"))
        self.assertIs(type(error), E.RateLimitError)
        self.assertTrue(error.retryable)

    def test_500_and_529_are_provider_error_and_retryable(self):
        for exc in (
            InternalServerError("oops", type="api_error"),
            APIStatusError("overloaded", status_code=529, type="overloaded_error"),
        ):
            with self.subTest(status=exc.status_code):
                error = self.call_error(exc)
                self.assertIs(type(error), E.ProviderError)
                self.assertTrue(error.retryable)

    def test_unlisted_status_is_unknown_error(self):
        error = self.call_error(APIStatusError("teapot", status_code=418))
        self.assertIs(type(error), E.UnknownLLMError)

    def test_T30_unexpected_sdk_exception_becomes_unknown_error(self):
        for exc in (RuntimeError("boom"), KeyError("x"), ValueError("v"), APIError("generic")):
            with self.subTest(exc=type(exc).__name__):
                error = self.call_error(exc)
                self.assertIs(type(error), E.UnknownLLMError)
                self.assertFalse(error.retryable)

    def test_the_raised_error_does_not_carry_the_sdk_exception(self):
        error = self.call_error(RuntimeError(f"secret {FAKE_KEY}"))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


# --- client configuration -------------------------------------------------


class ClientConfigTests(AdapterTestCase):
    def assert_client_config(self, client):
        self.assertEqual(client.kwargs["max_retries"], 0)
        self.assertEqual(client.kwargs["api_key"], FAKE_KEY)
        timeout = client.kwargs["timeout"]
        self.assertIsInstance(timeout, FakeTimeout)
        self.assertEqual(timeout.timeout, 180.0)
        self.assertEqual(timeout.connect, 10.0)

    def test_T32_client_has_no_retries_and_explicit_timeouts(self):
        adapter = self.adapter([message()])
        adapter.complete(request())
        self.assertEqual(len(self.sdk.clients), 1)
        self.assert_client_config(self.sdk.clients[0])

    def test_T32_verify_key_client_has_no_retries_and_explicit_timeouts(self):
        adapter = self.adapter(key=None)
        adapter.verify_key(FAKE_KEY)
        self.assertEqual(len(self.sdk.clients), 1)
        self.assert_client_config(self.sdk.clients[0])

    def test_the_client_is_built_once_per_adapter(self):
        adapter = self.adapter([message(), message()])
        adapter.complete(request())
        adapter.complete(request())
        self.assertEqual(len(self.sdk.clients), 1)

    def test_verify_key_retrieves_the_table_model_and_stores_nothing(self):
        adapter = self.adapter(key=None)
        adapter.verify_key(FAKE_KEY)
        self.assertEqual(self.sdk.retrieve_calls, [(("claude-haiku-5-5",), {})])
        self.assertEqual(self.sdk.create_calls, [])
        self.assertNotIn(FAKE_KEY, repr(vars(adapter).get("_keystore")))
        self.assertIsNone(adapter._client)

    def test_verify_key_failure_is_normalized(self):
        adapter = self.adapter(retrieve=[AuthenticationError(f"bad {FAKE_KEY}")], key=None)
        with self.assertRaises(E.AuthError) as caught:
            adapter.verify_key(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        adapter = self.adapter(retrieve=[RuntimeError("boom")], key=None)
        with self.assertRaises(E.UnknownLLMError):
            adapter.verify_key(FAKE_KEY)


# --- success path ---------------------------------------------------------


class SuccessTests(AdapterTestCase):
    def test_request_is_translated_to_the_messages_call(self):
        adapter = self.adapter([message()])
        req = request(max_output_tokens=1234)
        adapter.complete(req)
        (call,) = self.sdk.create_calls
        self.assertEqual(
            call,
            {
                "model": "claude-haiku-5-5",
                "max_tokens": 1234,
                "system": "SYSTEM PROMPT",
                "messages": [{"role": "user", "content": "<transcript>testo</transcript>"}],
                "output_config": {"effort": "low"},
            },
        )
        for forbidden in ("thinking", "temperature", "top_p", "top_k"):
            self.assertNotIn(forbidden, call)

    def test_success_joins_text_blocks_and_ignores_thinking(self):
        response = message(
            content=[thinking_block(), text_block("Prima. "), thinking_block(), text_block("Seconda.")]
        )
        result = self.adapter([response]).complete(request())
        self.assertIsInstance(result, LLMResponse)
        self.assertEqual(result.text, "Prima. Seconda.")
        self.assertEqual(result.status, "complete")

    def test_usage_and_cost_are_exact(self):
        result = self.adapter([message(input_tokens=12_345, output_tokens=6_789)]).complete(request())
        self.assertEqual(result.usage, Usage(12_345, 6_789))
        expected = Decimal(12_345) * Decimal("0.10") / 1_000_000 + Decimal(6_789) * Decimal("0.50") / 1_000_000
        self.assertEqual(result.cost.amount, expected)
        self.assertEqual(result.cost, compute_cost("claude-haiku-5-5", result.usage))
        self.assertEqual(result.cost.currency, "USD")
        self.assertIsInstance(result.latency_ms, int)
        self.assertGreaterEqual(result.latency_ms, 0)

    def test_latency_is_measured_with_the_monotonic_clock(self):
        adapter = self.adapter([message()])
        with mock.patch("meeting_recorder.llm.adapters.anthropic.time.monotonic", side_effect=[10.0, 10.25]):
            result = adapter.complete(request())
        self.assertEqual(result.latency_ms, 250)

    def test_max_tokens_stop_is_truncated_success(self):
        result = self.adapter([message(stop_reason="max_tokens")]).complete(request())
        self.assertEqual(result.status, "truncated")
        self.assertEqual(result.text, "## Sintesi\nOk.")

    def test_refusal_is_content_filtered_with_usage_and_not_retryable(self):
        response = message(content=[], stop_reason="refusal", input_tokens=200, output_tokens=3)
        adapter = self.adapter([response])
        with self.assertRaises(E.ContentFilteredError) as caught:
            adapter.complete(request())
        error = caught.exception
        self.assertFalse(error.retryable)
        self.assertEqual(error.usage, Usage(200, 3))
        self.assertEqual(error.cost, compute_cost("claude-haiku-5-5", Usage(200, 3)))

    def test_no_text_is_invalid_response_with_usage(self):
        for content in ([], [thinking_block()], [text_block("   ")]):
            with self.subTest(content=len(content)):
                adapter = self.adapter([message(content=content)])
                with self.assertRaises(E.InvalidResponseError) as caught:
                    adapter.complete(request())
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.usage, Usage(1000, 500))
                self.assertIsNotNone(caught.exception.cost.amount)

    def test_malformed_response_is_invalid_response(self):
        adapter = self.adapter([object()])
        with self.assertRaises(E.InvalidResponseError):
            adapter.complete(request())

    def test_missing_usage_gives_unknown_cost_never_zero(self):
        response = SimpleNamespace(content=[text_block("ok")], stop_reason="end_turn", usage=None)
        result = self.adapter([response]).complete(request())
        self.assertEqual(result.usage, Usage(None, None))
        self.assertIsNone(result.cost.amount)


# --- secrets, configuration, SDK availability -----------------------------


class SecretTests(AdapterTestCase):
    def test_T35_the_key_never_appears_in_errors_or_the_adapter_repr(self):
        leaky = f"Invalid key {FAKE_KEY} for request {FAKE_KEY}"
        exceptions = [
            AuthenticationError(leaky, body={"error": {"message": leaky}}, type="authentication_error"),
            BadRequestError(leaky, body={"error": {"message": leaky}}),
            RateLimitError(leaky),
            InternalServerError(leaky),
            APITimeoutError(leaky),
            APIConnectionError(leaky),
            RuntimeError(leaky),
            credit_balance_error(),
        ]
        exceptions[-1].message += " " + FAKE_KEY
        for exc in exceptions:
            with self.subTest(exc=type(exc).__name__):
                adapter = self.adapter([exc])
                with self.assertRaises(E.LLMError) as caught:
                    adapter.complete(request())
                error = caught.exception
                for text in (str(error), repr(error), error.user_message, repr(adapter), str(adapter)):
                    self.assertNotIn(FAKE_KEY, text)
                self.assertNotIn(FAKE_KEY, repr(error.args))

    def test_T35_the_key_is_not_in_the_response_translation_errors(self):
        adapter = self.adapter([message(content=[])])
        with self.assertRaises(E.InvalidResponseError) as caught:
            adapter.complete(request())
        self.assertNotIn(FAKE_KEY, repr(caught.exception))

    def test_missing_key_at_call_time_is_auth_error(self):
        adapter = self.adapter([message()], key=None)
        with self.assertRaises(E.AuthError):
            adapter.complete(request())
        self.assertEqual(self.sdk.clients, [])
        self.assertEqual(self.sdk.create_calls, [])


class ConfiguredAndSdkTests(unittest.TestCase):
    def test_T47_is_configured_follows_the_key_and_never_imports_the_sdk(self):
        store = InMemoryKeyStore()
        with mock.patch.dict(sys.modules):
            sys.modules.pop("anthropic", None)
            adapter = AnthropicAdapter(store)
            self.assertFalse(adapter.is_configured())
            store["anthropic"] = FAKE_KEY
            self.assertTrue(adapter.is_configured())
            self.assertNotIn("anthropic", sys.modules)
            self.assertIsNone(adapter._sdk)

    def test_missing_sdk_is_unknown_error(self):
        store = InMemoryKeyStore(anthropic=FAKE_KEY)
        adapter = AnthropicAdapter(store)
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            with self.assertRaises(E.UnknownLLMError):
                adapter.complete(request())
            with self.assertRaises(E.UnknownLLMError):
                adapter.verify_key(FAKE_KEY)

    def test_the_sdk_is_imported_lazily_on_first_use(self):
        fake = make_fake_sdk([message()])
        store = InMemoryKeyStore(anthropic=FAKE_KEY)
        adapter = AnthropicAdapter(store)
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            result = adapter.complete(request())
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(fake.clients), 1)


# --- registry and flow ----------------------------------------------------


class RegistryAndFlowTests(unittest.TestCase):
    def test_default_registry_creates_the_anthropic_adapter(self):
        registry = default_registry()
        self.assertEqual(registry.providers(), ("anthropic",))
        store = InMemoryKeyStore(anthropic=FAKE_KEY)
        adapter = registry.create("anthropic", store)
        self.assertIsInstance(adapter, AnthropicAdapter)
        self.assertTrue(adapter.is_configured())
        self.assertIsNone(registry.create("openai", store))

    def test_startup_check_reports_exactly_openai_and_oci_unregistered(self):
        problems = startup_check(registry=default_registry())
        self.assertEqual(
            sorted(problems),
            ["adapter not registered: oci", "adapter not registered: openai"],
        )

    def run_flow(self, create):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = Harness(Path(tmp.name), providers=("anthropic",), choices=[CHOOSE_ANTHROPIC_IT])
        fake = make_fake_sdk(create)
        registry = default_registry()
        registry.register("anthropic", lambda store: AnthropicAdapter(store, sdk=fake))
        h.registry = registry
        return h, fake, h.run()

    def test_the_flow_with_the_real_adapter_and_a_fake_sdk_reaches_done(self):
        h, fake, outcome = self.run_flow([message(input_tokens=2000, output_tokens=300)])
        self.assertEqual(outcome.kind, OutcomeKind.DONE)
        self.assertEqual(outcome.status, "complete")
        (call,) = fake.create_calls
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["max_tokens"], 16000)
        (record,) = h.saved_records()
        self.assertIn("Sintesi", str(record))
        self.assertNotIn(FAKE_KEY, h.all_text_on_disk())
        self.assertNotIn(FAKE_KEY, repr(outcome))

    def test_the_flow_ends_in_error_on_an_insufficient_credit_without_retry_offer(self):
        h, fake, outcome = self.run_flow([credit_balance_error()])
        self.assertEqual(outcome.kind, OutcomeKind.END_ERROR)
        self.assertEqual(outcome.reason, "InsufficientCreditsError")
        self.assertEqual(h.ui.retry_screens, [])
        self.assertEqual(len(fake.create_calls), 1)


# --- the fake stays honest ------------------------------------------------


@unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic SDK not installed")
class RealSdkShapeTests(unittest.TestCase):
    def test_the_real_sdk_has_the_names_the_fake_mirrors(self):
        import anthropic

        anthropic.Anthropic(
            api_key=FAKE_KEY, max_retries=0, timeout=anthropic.Timeout(180.0, connect=10.0)
        )
        for name in (
            "APITimeoutError",
            "APIConnectionError",
            "APIStatusError",
            "RateLimitError",
            "InternalServerError",
            "BadRequestError",
            "AuthenticationError",
            "PermissionDeniedError",
            "NotFoundError",
        ):
            self.assertTrue(hasattr(anthropic, name), name)
        self.assertTrue(issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError))
        self.assertTrue(issubclass(anthropic.BadRequestError, anthropic.APIStatusError))


if __name__ == "__main__":
    unittest.main()
