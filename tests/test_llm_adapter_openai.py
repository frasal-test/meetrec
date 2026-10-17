"""OpenAI-compatible adapter (OpenAI and Oracle OCI) against a fake `openai`
module: no network, no real SDK call, no real key (CLAUDE.md)."""
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_support import FAKE_KEY, Harness  # noqa: E402

from meeting_recorder.config import SummaryConfig  # noqa: E402
from meeting_recorder.llm import errors as E  # noqa: E402
from meeting_recorder.llm.adapters import default_registry  # noqa: E402
from meeting_recorder.llm.adapters import common  # noqa: E402
from meeting_recorder.llm.adapters.openai_compat import (  # noqa: E402
    OCI_SUPPORTED_REGIONS,
    OpenAICompatibleAdapter,
    make_oci_adapter,
    make_openai_adapter,
)
from meeting_recorder.llm.checks import startup_check  # noqa: E402
from meeting_recorder.llm.keys import InMemoryKeyStore  # noqa: E402
from meeting_recorder.llm.models import compute_cost, find_model  # noqa: E402
from meeting_recorder.llm.outcomes import OutcomeKind  # noqa: E402
from meeting_recorder.llm.types import LLMRequest, LLMResponse, Usage  # noqa: E402
from meeting_recorder.llm.ui import Choice  # noqa: E402

OCI_URL = "https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com/20231130/actions/v1"


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

    def __init__(self, message: str, *, status_code=None, body=None, code=None, type=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code or self.status_code_default
        self.body = body
        self.code = code
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


class FakeOpenAIClient:
    """Captures the constructor kwargs; `chat.completions.create` and
    `models.retrieve` return or raise the next scripted item."""

    def __init__(self, sdk, **kwargs):
        self.kwargs = kwargs
        self.sdk = sdk
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
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
    sdk = types.ModuleType("openai")
    sdk.create_script = list(create or [])
    sdk.retrieve_script = list(retrieve if retrieve is not None else [SimpleNamespace()])
    sdk.create_calls = []
    sdk.retrieve_calls = []
    sdk.clients = []

    def openai_factory(**kwargs):
        client = FakeOpenAIClient(sdk, **kwargs)
        sdk.clients.append(client)
        return client

    sdk.OpenAI = openai_factory
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


def completion(
    content="## Sintesi\nOk.",
    finish_reason="stop",
    refusal=None,
    prompt_tokens=1000,
    completion_tokens=500,
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=refusal),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


OPENAI_MODEL = "gpt-6-luna"
OCI_MODEL = "openai.gpt-oss-120b"


def request(provider="openai", **overrides) -> LLMRequest:
    values = dict(
        run_id="run-1",
        provider=provider,
        model_id=OPENAI_MODEL if provider == "openai" else OCI_MODEL,
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
    provider = "openai"

    def adapter(self, create=None, retrieve=None, key=FAKE_KEY, provider=None, region="eu-frankfurt-1"):
        provider = provider or self.provider
        self.sdk = make_fake_sdk(create, retrieve)
        store = InMemoryKeyStore()
        if key:
            store[provider] = key
        if provider == "oci":
            return make_oci_adapter(store, region, sdk=self.sdk)
        return make_openai_adapter(store, sdk=self.sdk)

    def call_error(self, exc, **kwargs) -> E.LLMError:
        adapter = self.adapter([exc], **kwargs)
        with self.assertRaises(E.LLMError) as caught:
            adapter.complete(request(adapter.provider))
        return caught.exception


def quota_error(**kwargs):
    text = "You exceeded your current quota, please check your plan and billing details."
    return RateLimitError(
        text,
        code="insufficient_quota",
        type="insufficient_quota",
        body={"code": "insufficient_quota", "type": "insufficient_quota", "message": text},
        **kwargs,
    )


# --- shared constants and the table --------------------------------------


class SharedPiecesTests(unittest.TestCase):
    def test_common_constants_are_shared_with_the_anthropic_adapter(self):
        from meeting_recorder.llm.adapters import anthropic as anthropic_adapter

        self.assertEqual(common.CONNECT_TIMEOUT_SECONDS, 10.0)
        self.assertEqual(common.READ_TIMEOUT_SECONDS, 180.0)
        for name in ("CONNECT_TIMEOUT_SECONDS", "READ_TIMEOUT_SECONDS", "CONNECTION_MESSAGE"):
            self.assertIs(getattr(anthropic_adapter, name), getattr(common, name))

    def test_common_imports_no_sdk(self):
        tree = ast.parse(Path(common.__file__).read_text(encoding="utf-8"))
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        self.assertEqual(imported, ["__future__"])

    def test_the_openai_row_is_verified(self):
        spec = find_model(OPENAI_MODEL)
        self.assertEqual(spec.context_window, 1_050_000)
        self.assertEqual(spec.long_context_threshold, 272_000)

    def test_supported_oci_regions(self):
        self.assertEqual(
            set(OCI_SUPPORTED_REGIONS),
            {
                "eu-frankfurt-1",
                "ap-hyderabad-1",
                "ap-osaka-1",
                "us-ashburn-1",
                "us-chicago-1",
                "us-phoenix-1",
            },
        )


# --- errors ---------------------------------------------------------------


class ErrorMappingTests(AdapterTestCase):
    def test_T26_insufficient_quota_429_is_insufficient_credits(self):
        error = self.call_error(quota_error())
        self.assertIsInstance(error, E.InsufficientCreditsError)
        self.assertFalse(error.retryable)
        self.assertIsNone(error.usage)
        self.assertIsNone(error.cost)

    def test_T26_insufficient_quota_is_found_in_the_body_fallbacks(self):
        cases = {
            "wrapped error code": {"error": {"code": "insufficient_quota"}},
            "flat code": {"code": "insufficient_quota"},
            "wrapped error type": {"error": {"type": "insufficient_quota"}},
        }
        for label, body in cases.items():
            with self.subTest(label):
                error = self.call_error(RateLimitError("Error code: 429", body=body))
                self.assertIsInstance(error, E.InsufficientCreditsError)
        error = self.call_error(RateLimitError("x", type="insufficient_quota"))
        self.assertIsInstance(error, E.InsufficientCreditsError)

    def test_T26_the_same_mapping_applies_to_oci(self):
        error = self.call_error(quota_error(), provider="oci")
        self.assertIsInstance(error, E.InsufficientCreditsError)
        self.assertFalse(error.retryable)

    def test_T27_plain_429_is_rate_limit(self):
        for exc in (
            RateLimitError("slow down", code="rate_limit_exceeded", body={"code": "rate_limit_exceeded"}),
            RateLimitError("slow down"),
            RateLimitError("slow down", body={"error": {"code": "rate_limit_exceeded"}}),
        ):
            for provider in ("openai", "oci"):
                with self.subTest(provider=provider):
                    error = self.call_error(exc, provider=provider)
                    self.assertIs(type(error), E.RateLimitError)
                    self.assertTrue(error.retryable)

    def test_insufficient_quota_code_on_another_status_is_not_credit(self):
        error = self.call_error(APIStatusError("x", status_code=400, code="insufficient_quota"))
        self.assertIs(type(error), E.BadRequestError)

    def test_timeout_is_timeout_error_and_retryable(self):
        error = self.call_error(APITimeoutError("Request timed out."))
        self.assertIs(type(error), E.TimeoutLLMError)
        self.assertTrue(error.retryable)
        self.assertNotIn("connection", error.user_message)

    def test_connection_error_is_a_retryable_timeout_with_the_connection_message(self):
        for provider in ("openai", "oci"):
            with self.subTest(provider=provider):
                error = self.call_error(APIConnectionError("Connection error."), provider=provider)
                self.assertIs(type(error), E.TimeoutLLMError)
                self.assertTrue(error.retryable)
                self.assertEqual(error.user_message, common.CONNECTION_MESSAGE)

    def test_400_413_422_are_bad_request(self):
        for exc in (
            BadRequestError("bad", code="invalid_value", body={"code": "invalid_value"}),
            APIStatusError("too big", status_code=413),
            UnprocessableEntityError("bad"),
        ):
            with self.subTest(status=exc.status_code):
                error = self.call_error(exc)
                self.assertIs(type(error), E.BadRequestError)
                self.assertFalse(error.retryable)

    def test_401_and_403_are_auth_error(self):
        for exc in (AuthenticationError("bad key"), PermissionDeniedError("nope")):
            with self.subTest(status=exc.status_code):
                error = self.call_error(exc)
                self.assertIs(type(error), E.AuthError)
                self.assertFalse(error.retryable)

    def test_404_is_model_not_found(self):
        error = self.call_error(NotFoundError("model"))
        self.assertIs(type(error), E.ModelNotFoundError)
        self.assertFalse(error.retryable)

    def test_5xx_is_provider_error_and_retryable(self):
        for exc in (InternalServerError("oops"), APIStatusError("bad gateway", status_code=502)):
            with self.subTest(status=exc.status_code):
                error = self.call_error(exc)
                self.assertIs(type(error), E.ProviderError)
                self.assertTrue(error.retryable)

    def test_unlisted_status_is_unknown_error(self):
        for status in (402, 409, 418):
            with self.subTest(status=status):
                error = self.call_error(APIStatusError("x", status_code=status))
                self.assertIs(type(error), E.UnknownLLMError)

    def test_T30_unexpected_sdk_exception_becomes_unknown_error(self):
        for provider in ("openai", "oci"):
            for exc in (RuntimeError("boom"), KeyError("x"), ValueError("v"), APIError("generic")):
                with self.subTest(provider=provider, exc=type(exc).__name__):
                    error = self.call_error(exc, provider=provider)
                    self.assertIs(type(error), E.UnknownLLMError)
                    self.assertFalse(error.retryable)

    def test_the_raised_error_does_not_carry_the_sdk_exception(self):
        error = self.call_error(RuntimeError(f"secret {FAKE_KEY}"))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_a_classification_failure_is_unknown_error(self):
        class Weird(APIStatusError):
            @property
            def status_code(self):
                raise RuntimeError("boom")

            @status_code.setter
            def status_code(self, value):
                pass

        self.assertIs(type(self.call_error(Weird("x"))), E.UnknownLLMError)


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
        for provider in ("openai", "oci"):
            with self.subTest(provider=provider):
                adapter = self.adapter([completion()], provider=provider)
                adapter.complete(request(provider))
                self.assertEqual(len(self.sdk.clients), 1)
                self.assert_client_config(self.sdk.clients[0])

    def test_T32_verify_key_clients_have_no_retries_and_explicit_timeouts(self):
        for provider in ("openai", "oci"):
            with self.subTest(provider=provider):
                adapter = self.adapter([completion()], key=None, provider=provider)
                adapter.verify_key(FAKE_KEY)
                self.assertEqual(len(self.sdk.clients), 1)
                self.assert_client_config(self.sdk.clients[0])

    def test_openai_client_uses_the_official_base_url(self):
        self.adapter([completion()]).complete(request())
        self.assertEqual(
            self.sdk.clients[0].kwargs["base_url"], "https://api.openai.com/v1"
        )

    def test_oci_client_gets_the_regional_base_url(self):
        self.adapter([completion()], provider="oci").complete(request("oci"))
        self.assertEqual(self.sdk.clients[0].kwargs["base_url"], OCI_URL)

    def test_every_supported_region_builds_its_official_endpoint(self):
        for region in OCI_SUPPORTED_REGIONS:
            with self.subTest(region=region):
                self.adapter([completion()], provider="oci", region=region).complete(request("oci"))
                self.assertEqual(
                    self.sdk.clients[0].kwargs["base_url"],
                    f"https://inference.generativeai.{region}.oci.oraclecloud.com/20231130/actions/v1",
                )

    def test_the_client_is_built_once_per_adapter_and_rebuilt_for_a_new_key(self):
        adapter = self.adapter([completion(), completion(), completion()])
        adapter.complete(request())
        adapter.complete(request())
        self.assertEqual(len(self.sdk.clients), 1)
        adapter._keystore["openai"] = "sk-FAKE-OTHER-KEY"
        adapter.complete(request())
        self.assertEqual(len(self.sdk.clients), 2)

    def test_openai_verify_key_retrieves_the_table_model_and_bills_nothing(self):
        adapter = self.adapter(key=None)
        adapter.verify_key(FAKE_KEY)
        self.assertEqual(self.sdk.retrieve_calls, [((OPENAI_MODEL,), {})])
        self.assertEqual(self.sdk.create_calls, [])
        self.assertIsNone(adapter._client)

    def test_oci_verify_key_is_a_minimal_chat_completion(self):
        adapter = self.adapter([completion(content="", finish_reason="length")], key=None, provider="oci")
        adapter.verify_key(FAKE_KEY)
        self.assertEqual(self.sdk.retrieve_calls, [])
        self.assertEqual(
            self.sdk.create_calls,
            [
                {
                    "model": OCI_MODEL,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 16,
                }
            ],
        )
        self.assertIsNone(adapter._client)

    def test_verify_key_failures_are_normalized(self):
        adapter = self.adapter(retrieve=[AuthenticationError(f"bad {FAKE_KEY}")], key=None)
        with self.assertRaises(E.AuthError) as caught:
            adapter.verify_key(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        adapter = self.adapter(retrieve=[RuntimeError("boom")], key=None)
        with self.assertRaises(E.UnknownLLMError):
            adapter.verify_key(FAKE_KEY)
        adapter = self.adapter([quota_error()], key=None, provider="oci")
        with self.assertRaises(E.InsufficientCreditsError):
            adapter.verify_key(FAKE_KEY)
        adapter = self.adapter([AuthenticationError("bad")], key=None, provider="oci")
        with self.assertRaises(E.AuthError):
            adapter.verify_key(FAKE_KEY)

    def test_verify_key_without_a_key_is_auth_error(self):
        with self.assertRaises(E.AuthError):
            self.adapter(key=None).verify_key("")

    def test_oci_verify_key_without_a_valid_region_is_an_error_and_makes_no_call(self):
        adapter = self.adapter(key=None, provider="oci", region=None)
        with self.assertRaises(E.UnknownLLMError):
            adapter.verify_key(FAKE_KEY)
        self.assertEqual(self.sdk.clients, [])


# --- success path ---------------------------------------------------------


class RequestTranslationTests(AdapterTestCase):
    FORBIDDEN = ("temperature", "top_p", "top_logprobs", "logprobs")

    def test_openai_request_kwargs_are_exact(self):
        adapter = self.adapter([completion()])
        adapter.complete(request(max_output_tokens=1234))
        (call,) = self.sdk.create_calls
        self.assertEqual(
            call,
            {
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": "SYSTEM PROMPT"},
                    {"role": "user", "content": "<transcript>testo</transcript>"},
                ],
                "max_completion_tokens": 1234,
                "reasoning_effort": "low",
            },
        )
        for forbidden in self.FORBIDDEN + ("max_tokens",):
            self.assertNotIn(forbidden, call)

    def test_oci_request_kwargs_are_exact(self):
        adapter = self.adapter([completion()], provider="oci")
        adapter.complete(request("oci", max_output_tokens=1234))
        (call,) = self.sdk.create_calls
        self.assertEqual(
            call,
            {
                "model": OCI_MODEL,
                "messages": [
                    {"role": "system", "content": "SYSTEM PROMPT"},
                    {"role": "user", "content": "<transcript>testo</transcript>"},
                ],
                "max_tokens": 1234,
            },
        )
        for forbidden in self.FORBIDDEN + ("reasoning_effort", "max_completion_tokens"):
            self.assertNotIn(forbidden, call)


class ResponseTranslationTests(AdapterTestCase):
    def test_stop_is_complete(self):
        for provider in ("openai", "oci"):
            with self.subTest(provider=provider):
                result = self.adapter([completion()], provider=provider).complete(request(provider))
                self.assertIsInstance(result, LLMResponse)
                self.assertEqual(result.text, "## Sintesi\nOk.")
                self.assertEqual(result.status, "complete")

    def test_length_is_truncated_success(self):
        result = self.adapter([completion(finish_reason="length")]).complete(request())
        self.assertEqual(result.status, "truncated")
        self.assertEqual(result.text, "## Sintesi\nOk.")

    def test_length_without_text_is_invalid_response(self):
        adapter = self.adapter([completion(content=None, finish_reason="length")])
        with self.assertRaises(E.InvalidResponseError) as caught:
            adapter.complete(request())
        self.assertEqual(caught.exception.usage, Usage(1000, 500))

    def test_content_filter_is_content_filtered_with_usage_and_not_retryable(self):
        adapter = self.adapter(
            [completion(content=None, finish_reason="content_filter", prompt_tokens=200, completion_tokens=3)]
        )
        with self.assertRaises(E.ContentFilteredError) as caught:
            adapter.complete(request())
        error = caught.exception
        self.assertFalse(error.retryable)
        self.assertEqual(error.usage, Usage(200, 3))
        self.assertEqual(error.cost, compute_cost(OPENAI_MODEL, Usage(200, 3)))

    def test_refusal_string_is_content_filtered_even_with_stop(self):
        for provider in ("openai", "oci"):
            with self.subTest(provider=provider):
                adapter = self.adapter(
                    [completion(content=None, refusal="I cannot help with that.")], provider=provider
                )
                with self.assertRaises(E.ContentFilteredError) as caught:
                    adapter.complete(request(provider))
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.usage, Usage(1000, 500))

    def test_empty_refusal_is_not_a_refusal(self):
        result = self.adapter([completion(refusal="")]).complete(request())
        self.assertEqual(result.status, "complete")

    def test_empty_content_is_invalid_response_with_usage(self):
        for content in (None, "", "   \n"):
            with self.subTest(content=content):
                adapter = self.adapter([completion(content=content)])
                with self.assertRaises(E.InvalidResponseError) as caught:
                    adapter.complete(request())
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.usage, Usage(1000, 500))
                self.assertIsNotNone(caught.exception.cost.amount)

    def test_malformed_response_is_invalid_response(self):
        for response in (object(), SimpleNamespace(choices=[], usage=None), SimpleNamespace(choices=None)):
            with self.subTest(response=type(response).__name__):
                adapter = self.adapter([response])
                with self.assertRaises(E.InvalidResponseError):
                    adapter.complete(request())

    def test_usage_and_cost_are_exact(self):
        result = self.adapter([completion(prompt_tokens=12_345, completion_tokens=6_789)]).complete(request())
        self.assertEqual(result.usage, Usage(12_345, 6_789))
        expected = Decimal(12_345) * Decimal("0.10") / 1_000_000 + Decimal(6_789) * Decimal("0.50") / 1_000_000
        self.assertEqual(result.cost.amount, expected)
        self.assertEqual(result.cost, compute_cost(OPENAI_MODEL, result.usage))
        self.assertEqual(result.cost.currency, "USD")
        self.assertIsInstance(result.latency_ms, int)

    def test_oci_cost_uses_the_oci_prices(self):
        result = self.adapter(
            [completion(prompt_tokens=10_000, completion_tokens=2_000)], provider="oci"
        ).complete(request("oci"))
        expected = Decimal(10_000) * Decimal("0.15") / 1_000_000 + Decimal(2_000) * Decimal("0.60") / 1_000_000
        self.assertEqual(result.cost.amount, expected)

    def test_missing_usage_gives_unknown_cost_never_zero(self):
        response = completion()
        response.usage = None
        result = self.adapter([response]).complete(request())
        self.assertEqual(result.usage, Usage(None, None))
        self.assertIsNone(result.cost.amount)

    def test_latency_is_measured_with_the_monotonic_clock(self):
        adapter = self.adapter([completion()])
        with mock.patch(
            "meeting_recorder.llm.adapters.openai_compat.time.monotonic", side_effect=[10.0, 10.25]
        ):
            result = adapter.complete(request())
        self.assertEqual(result.latency_ms, 250)


# --- secrets, configuration, SDK availability -----------------------------


class SecretTests(AdapterTestCase):
    def test_T35_the_key_never_appears_in_errors_or_the_adapter_repr(self):
        leaky = f"Incorrect API key provided: {FAKE_KEY} for request {FAKE_KEY}"
        body = {"error": {"message": leaky, "code": "invalid_api_key"}}
        exceptions = [
            AuthenticationError(leaky, body=body, code="invalid_api_key"),
            BadRequestError(leaky, body=body),
            RateLimitError(leaky, body=body),
            quota_error(),
            InternalServerError(leaky),
            APITimeoutError(leaky),
            APIConnectionError(leaky),
            RuntimeError(leaky),
        ]
        exceptions[3].message += " " + FAKE_KEY
        for provider in ("openai", "oci"):
            for exc in exceptions:
                with self.subTest(provider=provider, exc=type(exc).__name__):
                    adapter = self.adapter([exc], provider=provider)
                    with self.assertRaises(E.LLMError) as caught:
                        adapter.complete(request(provider))
                    error = caught.exception
                    for text in (str(error), repr(error), error.user_message, repr(adapter), str(adapter)):
                        self.assertNotIn(FAKE_KEY, text)
                    self.assertNotIn(FAKE_KEY, repr(error.args))

    def test_T35_the_key_is_not_in_the_response_translation_errors(self):
        adapter = self.adapter([completion(content=None)])
        with self.assertRaises(E.InvalidResponseError) as caught:
            adapter.complete(request())
        self.assertNotIn(FAKE_KEY, repr(caught.exception))

    def test_missing_key_at_call_time_is_auth_error(self):
        for provider in ("openai", "oci"):
            with self.subTest(provider=provider):
                adapter = self.adapter([completion()], key=None, provider=provider)
                with self.assertRaises(E.AuthError):
                    adapter.complete(request(provider))
                self.assertEqual(self.sdk.clients, [])
                self.assertEqual(self.sdk.create_calls, [])

    def test_oci_without_a_valid_region_makes_no_call(self):
        for region in (None, "", "eu-madrid-1"):
            with self.subTest(region=region):
                adapter = self.adapter([completion()], provider="oci", region=region)
                with self.assertRaises(E.UnknownLLMError):
                    adapter.complete(request("oci"))
                self.assertEqual(self.sdk.clients, [])


class ConfiguredAndSdkTests(unittest.TestCase):
    def test_T47_is_configured_follows_the_key_and_never_imports_the_sdk(self):
        store = InMemoryKeyStore()
        with mock.patch.dict(sys.modules):
            sys.modules.pop("openai", None)
            adapter = make_openai_adapter(store)
            self.assertFalse(adapter.is_configured())
            store["openai"] = FAKE_KEY
            self.assertTrue(adapter.is_configured())
            self.assertNotIn("openai", sys.modules)
            self.assertIsNone(adapter._sdk)

    def test_T47_oci_is_configured_only_with_a_key_and_a_supported_region(self):
        store = InMemoryKeyStore()
        with mock.patch.dict(sys.modules):
            sys.modules.pop("openai", None)
            self.assertFalse(make_oci_adapter(store, "eu-frankfurt-1").is_configured())
            store["oci"] = FAKE_KEY
            for region in OCI_SUPPORTED_REGIONS:
                self.assertTrue(make_oci_adapter(store, region).is_configured(), region)
            for region in (None, "", "   ", "eu-madrid-1", "EU-FRANKFURT-1", "https://evil.example"):
                self.assertFalse(make_oci_adapter(store, region).is_configured(), region)
            self.assertNotIn("openai", sys.modules)
            empty = InMemoryKeyStore()
            self.assertFalse(make_oci_adapter(empty, "eu-frankfurt-1").is_configured())

    def test_an_unsupported_region_does_not_crash_the_factory(self):
        adapter = make_oci_adapter(InMemoryKeyStore(oci=FAKE_KEY), "eu-madrid-1")
        self.assertIsInstance(adapter, OpenAICompatibleAdapter)
        self.assertFalse(adapter.is_configured())

    def test_missing_sdk_is_unknown_error(self):
        store = InMemoryKeyStore(openai=FAKE_KEY, oci=FAKE_KEY)
        with mock.patch.dict(sys.modules, {"openai": None}):
            for adapter, provider in (
                (make_openai_adapter(store), "openai"),
                (make_oci_adapter(store, "eu-frankfurt-1"), "oci"),
            ):
                with self.subTest(provider=provider):
                    with self.assertRaises(E.UnknownLLMError):
                        adapter.complete(request(provider))
                    with self.assertRaises(E.UnknownLLMError):
                        adapter.verify_key(FAKE_KEY)

    def test_the_sdk_is_imported_lazily_on_first_use(self):
        fake = make_fake_sdk([completion()])
        adapter = make_openai_adapter(InMemoryKeyStore(openai=FAKE_KEY))
        with mock.patch.dict(sys.modules, {"openai": fake}):
            result = adapter.complete(request())
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(fake.clients), 1)

    def test_the_adapter_repr_names_only_the_provider(self):
        self.assertEqual(
            repr(make_openai_adapter(InMemoryKeyStore(openai=FAKE_KEY))),
            "OpenAICompatibleAdapter(provider='openai')",
        )


# --- registry and flow ----------------------------------------------------


class RegistryAndFlowTests(unittest.TestCase):
    def test_default_registry_registers_the_three_providers(self):
        registry = default_registry()
        self.assertEqual(registry.providers(), ("anthropic", "openai", "oci"))
        store = InMemoryKeyStore(openai=FAKE_KEY, oci=FAKE_KEY)
        openai_adapter = registry.create("openai", store)
        self.assertIsInstance(openai_adapter, OpenAICompatibleAdapter)
        self.assertEqual(openai_adapter.provider, "openai")
        self.assertTrue(openai_adapter.is_configured())
        # No summary configuration: no region, so OCI is not configured.
        self.assertFalse(registry.create("oci", store).is_configured())

    def test_default_registry_takes_the_oci_region_from_the_summary_config(self):
        store = InMemoryKeyStore(oci=FAKE_KEY)
        registry = default_registry(SummaryConfig(oci_region="eu-frankfurt-1"))
        self.assertTrue(registry.create("oci", store).is_configured())
        registry = default_registry(SummaryConfig(oci_region="nowhere-1"))
        self.assertFalse(registry.create("oci", store).is_configured())
        registry = default_registry(SummaryConfig())
        self.assertFalse(registry.create("oci", store).is_configured())

    def test_startup_check_with_the_default_registry_finds_no_problems(self):
        self.assertEqual(startup_check(registry=default_registry()), [])

    def run_flow(self, provider, model_id, create, retries=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = Harness(
            Path(tmp.name),
            providers=(provider,),
            choices=[Choice(model_id, "it")],
            retries=list(retries),
        )
        fake = make_fake_sdk(create)
        registry = default_registry(SummaryConfig(oci_region="eu-frankfurt-1"))
        registry.register("openai", lambda store: make_openai_adapter(store, sdk=fake))
        registry.register("oci", lambda store: make_oci_adapter(store, "eu-frankfurt-1", sdk=fake))
        h.registry = registry
        return h, fake, h.run()

    def test_the_flow_with_the_openai_adapter_and_a_fake_sdk_reaches_done(self):
        h, fake, outcome = self.run_flow(
            "openai", OPENAI_MODEL, [completion(prompt_tokens=2000, completion_tokens=300)]
        )
        self.assertEqual(outcome.kind, OutcomeKind.DONE)
        self.assertEqual(outcome.status, "complete")
        (call,) = fake.create_calls
        self.assertEqual(call["model"], OPENAI_MODEL)
        self.assertEqual(call["max_completion_tokens"], 16000)
        (record,) = h.saved_records()
        self.assertIn("Sintesi", str(record))
        self.assertNotIn(FAKE_KEY, h.all_text_on_disk())
        self.assertNotIn(FAKE_KEY, repr(outcome))

    def test_the_flow_with_the_oci_adapter_and_a_fake_sdk_reaches_done(self):
        h, fake, outcome = self.run_flow("oci", OCI_MODEL, [completion()])
        self.assertEqual(outcome.kind, OutcomeKind.DONE)
        (call,) = fake.create_calls
        self.assertEqual(call["model"], OCI_MODEL)
        self.assertEqual(call["max_tokens"], 16000)
        self.assertEqual(fake.clients[0].kwargs["base_url"], OCI_URL)
        self.assertNotIn(FAKE_KEY, h.all_text_on_disk())

    def test_T26_the_flow_ends_in_error_on_insufficient_quota_without_retry_offer(self):
        for provider, model_id in (("openai", OPENAI_MODEL), ("oci", OCI_MODEL)):
            with self.subTest(provider=provider):
                h, fake, outcome = self.run_flow(provider, model_id, [quota_error()])
                self.assertEqual(outcome.kind, OutcomeKind.END_ERROR)
                self.assertEqual(outcome.reason, "InsufficientCreditsError")
                self.assertEqual(h.ui.retry_screens, [])
                self.assertEqual(len(fake.create_calls), 1)

    def test_T27_the_flow_offers_a_retry_on_a_plain_rate_limit(self):
        for provider, model_id in (("openai", OPENAI_MODEL), ("oci", OCI_MODEL)):
            with self.subTest(provider=provider):
                h, fake, outcome = self.run_flow(
                    provider,
                    model_id,
                    [RateLimitError("slow down", code="rate_limit_exceeded")],
                    retries=[False],
                )
                self.assertEqual(len(h.ui.retry_screens), 1)
                self.assertEqual(outcome.kind, OutcomeKind.END_ERROR)
                self.assertEqual(outcome.reason, "RateLimitError")
                self.assertEqual(len(fake.create_calls), 1)


# --- the fake stays honest ------------------------------------------------


@unittest.skipUnless(importlib.util.find_spec("openai"), "openai SDK not installed")
class RealSdkShapeTests(unittest.TestCase):
    NAMES = (
        "OpenAI",
        "Timeout",
        "APITimeoutError",
        "APIConnectionError",
        "APIStatusError",
        "RateLimitError",
        "BadRequestError",
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "InternalServerError",
        "UnprocessableEntityError",
    )

    def test_the_real_sdk_has_the_names_the_fake_mirrors(self):
        import openai

        for name in self.NAMES:
            self.assertTrue(hasattr(openai, name), name)
        self.assertTrue(issubclass(openai.APITimeoutError, openai.APIConnectionError))
        for name in (
            "RateLimitError",
            "BadRequestError",
            "AuthenticationError",
            "PermissionDeniedError",
            "NotFoundError",
            "InternalServerError",
            "UnprocessableEntityError",
        ):
            self.assertTrue(issubclass(getattr(openai, name), openai.APIStatusError), name)
        self.assertFalse(issubclass(openai.APIStatusError, openai.APIConnectionError))

    def test_the_real_client_is_built_as_the_adapter_builds_it(self):
        import openai

        client = openai.OpenAI(
            api_key=FAKE_KEY, max_retries=0, timeout=openai.Timeout(180.0, connect=10.0)
        )
        self.assertEqual(client.max_retries, 0)
        regional = openai.OpenAI(
            api_key=FAKE_KEY,
            base_url=OCI_URL,
            max_retries=0,
            timeout=openai.Timeout(180.0, connect=10.0),
        )
        self.assertEqual(str(regional.base_url).rstrip("/"), OCI_URL)
        params = inspect.signature(client.chat.completions.create).parameters
        for name in ("max_completion_tokens", "max_tokens", "reasoning_effort", "model", "messages"):
            self.assertIn(name, params)
        self.assertIn("model", inspect.signature(client.models.retrieve).parameters)

    def test_the_adapter_classifies_real_sdk_exceptions(self):
        import openai

        httpx = None
        for name in ("httpx", "httpx2"):
            if importlib.util.find_spec(name):
                httpx = importlib.import_module(name)
                break
        if httpx is None:
            self.skipTest("httpx not importable")
        http_request = httpx.Request("POST", "http://localhost/v1/chat/completions")

        def status(cls, code, body):
            return cls("boom", response=httpx.Response(code, request=http_request), body=body)

        cases = [
            (
                status(openai.RateLimitError, 429, {"code": "insufficient_quota", "type": "insufficient_quota"}),
                E.InsufficientCreditsError,
            ),
            (
                status(openai.RateLimitError, 429, {"error": {"code": "insufficient_quota"}}),
                E.InsufficientCreditsError,
            ),
            (status(openai.RateLimitError, 429, {"code": "rate_limit_exceeded"}), E.RateLimitError),
            (status(openai.BadRequestError, 400, None), E.BadRequestError),
            (status(openai.AuthenticationError, 401, None), E.AuthError),
            (status(openai.NotFoundError, 404, None), E.ModelNotFoundError),
            (status(openai.InternalServerError, 503, None), E.ProviderError),
            (openai.APITimeoutError(request=http_request), E.TimeoutLLMError),
            (openai.APIConnectionError(request=http_request), E.TimeoutLLMError),
        ]
        adapter = make_openai_adapter(InMemoryKeyStore(openai=FAKE_KEY), sdk=openai)
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertIs(type(adapter._normalize(openai, exc)), expected)


if __name__ == "__main__":
    unittest.main()
