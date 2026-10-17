"""Anthropic adapter (docs/flow/adapter-contract.md).

One of the only modules allowed to import a provider SDK (I9), and it does so
on first use, never at import time. Every exception that leaves it is an
LLMError: the SDK exception is classified on HTTP status AND body, then
dropped, because its message could carry request details.
"""
from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any

from .. import models
from ..errors import (
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
from ..keys import KeyStore
from ..types import LLMRequest, LLMResponse, Usage
from .common import (
    ANTHROPIC_BASE_URL,
    CONNECT_TIMEOUT_SECONDS,
    CONNECTION_MESSAGE,
    READ_TIMEOUT_SECONDS,
)

PROVIDER = "anthropic"


def _mentions_credit_balance(exc: Any) -> bool:
    """Anthropic reports an exhausted credit as a 400 (or 402 billing_error)
    whose message is about the credit balance, not as a 429. See "Nota credito
    esaurito" in docs/flow/adapter-contract.md."""
    texts = [str(getattr(exc, "message", "") or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            texts.append(str(error.get("message") or ""))
        texts.append(str(body.get("message") or ""))
    return any("credit balance" in text.lower() for text in texts)


def _error_type(exc: Any) -> str:
    kind = getattr(exc, "type", None)
    if not isinstance(kind, str):
        body = getattr(exc, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        kind = error.get("type") if isinstance(error, dict) else None
    return kind if isinstance(kind, str) else ""


def _classify_status(exc: Any) -> LLMError:
    status = getattr(exc, "status_code", None)
    if _error_type(exc) == "billing_error" or status == 402:
        return InsufficientCreditsError()
    if status == 400:
        if _mentions_credit_balance(exc):
            return InsufficientCreditsError()
        return BadRequestError()
    if status in (413, 422):
        return BadRequestError()
    if status in (401, 403):
        return AuthError()
    if status == 404:
        return ModelNotFoundError()
    if status == 429:
        return RateLimitError()
    if isinstance(status, int) and status >= 500:  # includes 529 overloaded
        return ProviderError()
    return UnknownLLMError()


class AnthropicAdapter:
    provider = PROVIDER

    def __init__(self, keystore: KeyStore, *, sdk: ModuleType | None = None) -> None:
        self._keystore = keystore
        # A module object injected by the tests; otherwise imported on first use.
        self._sdk = sdk
        self._client: Any = None
        self._client_key: str | None = None

    def __repr__(self) -> str:
        return "AnthropicAdapter()"

    def is_configured(self) -> bool:
        """Only reads the key store: no SDK import, no network (T47)."""
        return self._keystore.has_key(PROVIDER)

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Stato: CALL_PROVIDER (adapter Anthropic). Request -> call ->
        response -> cost. Raises only LLMError."""
        sdk = self._load_sdk()
        key = self._keystore.get_key(PROVIDER)
        if not key:
            raise AuthError()
        client = self._client_for(sdk, key)
        started = time.monotonic()
        error: LLMError | None = None
        response: Any = None
        try:
            response = client.messages.create(
                model=req.model_id,
                max_tokens=req.max_output_tokens,
                system=req.system_prompt,
                messages=[{"role": "user", "content": req.transcript}],
                # Thinking is adaptive and on by default for this model; no
                # `thinking`, sampling params or prefill (all rejected with 400).
                output_config={"effort": "low"},
            )
        except Exception as exc:  # noqa: BLE001 - normalized below
            error = self._normalize(sdk, exc)
        if error is not None:
            # Raised outside the except block: no __context__ keeps the SDK
            # exception (and whatever its message says) attached.
            raise error
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._translate(req, response, latency_ms)

    def verify_key(self, key: str) -> None:
        """Used by Settings when a key is saved (I4), outside the flow. A
        minimal authenticated call that bills no tokens. The key is not stored."""
        sdk = self._load_sdk()
        if not key:
            raise AuthError()
        client = self._new_client(sdk, key)
        spec = next(s for s in models.MODEL_TABLE if s.provider == PROVIDER)
        error: LLMError | None = None
        try:
            client.models.retrieve(spec.model_id)
        except Exception as exc:  # noqa: BLE001 - normalized below
            error = self._normalize(sdk, exc)
        if error is not None:
            raise error

    # -- internals ---------------------------------------------------------

    def _load_sdk(self) -> ModuleType:
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module("anthropic")
            except ImportError:
                # requirements-summary.txt is installed when the first key is saved.
                raise UnknownLLMError() from None
        return self._sdk

    def _new_client(self, sdk: Any, key: str) -> Any:
        # max_retries=0: the SDK retries 408/409/429/5xx and connection errors
        # by default, which would violate I10.
        return sdk.Anthropic(
            api_key=key,
            base_url=ANTHROPIC_BASE_URL,
            max_retries=0,
            timeout=sdk.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        )

    def _client_for(self, sdk: Any, key: str) -> Any:
        if self._client is None or self._client_key != key:
            self._client = self._new_client(sdk, key)
            self._client_key = key
        return self._client

    def _normalize(self, sdk: Any, exc: Exception) -> LLMError:
        """SDK exception -> normalized LLMError. The original message is
        never copied: the user messages are the classes' own."""
        if isinstance(exc, LLMError):
            return exc
        try:
            if isinstance(exc, sdk.APITimeoutError):
                return TimeoutLLMError()
            if isinstance(exc, sdk.APIConnectionError):
                # Offline, DNS or refused: retryable like a timeout
                # (adapter-contract.md).
                return TimeoutLLMError(user_message=CONNECTION_MESSAGE)
            if isinstance(exc, sdk.APIStatusError):
                return _classify_status(exc)
        except Exception:  # noqa: BLE001 - classification must never escape
            pass
        return UnknownLLMError()

    def _translate(self, req: LLMRequest, response: Any, latency_ms: int) -> LLMResponse:
        """Provider response -> LLMResponse: stop_reason -> complete/truncated
        or ContentFilteredError, then the cost from the shared function."""
        try:
            raw_usage = getattr(response, "usage", None)
            usage = Usage(
                input_tokens=getattr(raw_usage, "input_tokens", None),
                output_tokens=getattr(raw_usage, "output_tokens", None),
            )
            stop_reason = getattr(response, "stop_reason", None)
            # Blocks may start with empty `thinking` blocks: only text counts.
            text = "".join(
                block.text
                for block in (getattr(response, "content", None) or [])
                if getattr(block, "type", None) == "text"
                and isinstance(getattr(block, "text", None), str)
            )
        except Exception:  # noqa: BLE001 - malformed response
            raise InvalidResponseError() from None
        cost = models.compute_cost(req.model_id, usage)
        if stop_reason == "refusal":
            raise ContentFilteredError(usage=usage, cost=cost)
        if not text.strip():
            raise InvalidResponseError(usage=usage, cost=cost)
        return LLMResponse(
            text=text,
            status="truncated" if stop_reason == "max_tokens" else "complete",
            usage=usage,
            cost=cost,
            latency_ms=latency_ms,
        )
