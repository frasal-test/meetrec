"""OpenAI-compatible adapter, serving OpenAI and Oracle OCI Generative AI
(docs/flow/adapter-contract.md).

One of the only modules allowed to import a provider SDK (I9), and it does so
on first use, never at import time. One implementation of request/response
translation and error normalization; OpenAI and OCI differ only in
configuration (base URL, output-limit parameter, reasoning parameter, how a
key is verified). Every exception that leaves it is an LLMError: the SDK
exception is classified on HTTP status AND body, then dropped, because its
message could carry request details.
"""
from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any, Mapping

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
    CONNECT_TIMEOUT_SECONDS,
    CONNECTION_MESSAGE,
    OPENAI_BASE_URL,
    READ_TIMEOUT_SECONDS,
)

OPENAI_PROVIDER = "openai"
OCI_PROVIDER = "oci"

# Regions where the OCI Generative AI service key works with the OpenAI SDK
# (docs/llm-summary-plan.md, "Oracle OCI"). Any other region is rejected.
OCI_SUPPORTED_REGIONS: tuple[str, ...] = (
    "eu-frankfurt-1",
    "ap-hyderabad-1",
    "ap-osaka-1",
    "us-ashburn-1",
    "us-chicago-1",
    "us-phoenix-1",
)

_OCI_BASE_URL = "https://inference.generativeai.{region}.oci.oraclecloud.com/20231130/actions/v1"


def oci_base_url(region: str | None) -> str | None:
    """The official OCI inference endpoint for a supported region, else None."""
    if isinstance(region, str) and region.strip() in OCI_SUPPORTED_REGIONS:
        return _OCI_BASE_URL.format(region=region.strip())
    return None


def _quota_code(exc: Any) -> str:
    """Error code of the response. The SDK exposes it as `exc.code`; the body
    may be the error object itself or wrapped as {"error": {...}}."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        for source in (error, body):
            if isinstance(source, dict):
                value = source.get("code") or source.get("type")
                if isinstance(value, str) and value:
                    return value
    kind = getattr(exc, "type", None)
    return kind if isinstance(kind, str) else ""


def _classify_status(exc: Any) -> LLMError:
    status = getattr(exc, "status_code", None)
    if status == 429:
        # OpenAI reports an exhausted credit as a 429 with code
        # `insufficient_quota`: same status as the rate limit, so the body
        # decides ("Nota credito esaurito" in adapter-contract.md).
        # DA VERIFICARE su una tenancy OCI reale: how OCI signals an exhausted
        # credit is undocumented; the same generic mapping is applied.
        if _quota_code(exc) == "insufficient_quota":
            return InsufficientCreditsError()
        return RateLimitError()
    if status in (400, 413, 422):
        return BadRequestError()
    if status in (401, 403):
        return AuthError()
    if status == 404:
        return ModelNotFoundError()
    if isinstance(status, int) and status >= 500:
        return ProviderError()
    return UnknownLLMError()


class OpenAICompatibleAdapter:
    def __init__(
        self,
        keystore: KeyStore,
        *,
        provider: str,
        model_id: str,
        token_param: str,
        extra_params: Mapping[str, Any] | None = None,
        base_url: str | None = None,
        requires_base_url: bool = False,
        verify_with_ping: bool = False,
        sdk: ModuleType | None = None,
    ) -> None:
        self.provider = provider
        self._keystore = keystore
        self._model_id = model_id
        self._token_param = token_param
        self._extra_params = dict(extra_params or {})
        self._base_url = base_url
        # OCI has no default endpoint: without a valid region it is unusable.
        self._requires_base_url = requires_base_url
        self._verify_with_ping = verify_with_ping
        # A module object injected by the tests; otherwise imported on first use.
        self._sdk = sdk
        self._client: Any = None
        self._client_key: str | None = None

    def __repr__(self) -> str:
        return f"OpenAICompatibleAdapter(provider={self.provider!r})"

    def is_configured(self) -> bool:
        """Only reads the key store (and the region, for OCI): no SDK import,
        no network (T47)."""
        if self._requires_base_url and not self._base_url:
            return False
        return self._keystore.has_key(self.provider)

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Stato: CALL_PROVIDER (adapter OpenAI/OCI). Request -> call ->
        response -> cost. Raises only LLMError."""
        sdk = self._load_sdk()
        if self._requires_base_url and not self._base_url:
            raise UnknownLLMError()
        key = self._keystore.get_key(self.provider)
        if not key:
            raise AuthError()
        client = self._client_for(sdk, key)
        started = time.monotonic()
        error: LLMError | None = None
        response: Any = None
        try:
            response = client.chat.completions.create(
                model=req.model_id,
                messages=[
                    {"role": "system", "content": req.system_prompt},
                    {"role": "user", "content": req.transcript},
                ],
                **{self._token_param: req.max_output_tokens},
                **self._extra_params,
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
        """Used by Settings when a key is saved (I4), outside the flow. The key
        is not stored. OpenAI: `models.retrieve`, which bills no tokens. OCI has
        no documented /models endpoint: a minimal chat completion, where any
        successful response (even truncated or empty) means the key works."""
        sdk = self._load_sdk()
        if not key:
            raise AuthError()
        if self._requires_base_url and not self._base_url:
            raise UnknownLLMError()
        client = self._new_client(sdk, key)
        error: LLMError | None = None
        try:
            if self._verify_with_ping:
                # Verified on a real tenancy (eu-frankfurt-1, 17 Oct 2026):
                # max_tokens and this minimal payload are accepted.
                client.chat.completions.create(
                    model=self._model_id,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=16,
                )
            else:
                client.models.retrieve(self._model_id)
        except Exception as exc:  # noqa: BLE001 - normalized below
            error = self._normalize(sdk, exc)
        if error is not None:
            raise error

    # -- internals ---------------------------------------------------------

    def _load_sdk(self) -> ModuleType:
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module("openai")
            except ImportError:
                # requirements-summary.txt is installed when the first key is saved.
                raise UnknownLLMError() from None
        return self._sdk

    def _new_client(self, sdk: Any, key: str) -> Any:
        kwargs: dict[str, Any] = {}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        # max_retries=0: the SDK retries 408/409/429/5xx and connection errors
        # by default, which would violate I10.
        return sdk.OpenAI(
            api_key=key,
            max_retries=0,
            timeout=sdk.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            **kwargs,
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
        """Provider response -> LLMResponse: finish_reason -> complete/truncated
        or ContentFilteredError, then the cost from the shared function."""
        try:
            raw_usage = getattr(response, "usage", None)
            usage = Usage(
                input_tokens=getattr(raw_usage, "prompt_tokens", None),
                output_tokens=getattr(raw_usage, "completion_tokens", None),
            )
            choices = getattr(response, "choices", None)
            choice = choices[0] if choices else None
            finish_reason = getattr(choice, "finish_reason", None)
            msg = getattr(choice, "message", None)
            content = getattr(msg, "content", None)
            refusal = getattr(msg, "refusal", None)
            text = content if isinstance(content, str) else ""
            refused = isinstance(refusal, str) and bool(refusal.strip())
        except Exception:  # noqa: BLE001 - malformed response
            raise InvalidResponseError() from None
        cost = models.compute_cost(req.model_id, usage)
        if choice is None:
            raise InvalidResponseError(usage=usage, cost=cost)
        if finish_reason == "content_filter" or refused:
            raise ContentFilteredError(usage=usage, cost=cost)
        # Includes `length` with no text left (reasoning used the whole budget).
        if not text.strip():
            raise InvalidResponseError(usage=usage, cost=cost)
        return LLMResponse(
            text=text,
            status="truncated" if finish_reason == "length" else "complete",
            usage=usage,
            cost=cost,
            latency_ms=latency_ms,
        )


def make_openai_adapter(
    keystore: KeyStore, sdk: ModuleType | None = None
) -> OpenAICompatibleAdapter:
    """Stato: CALL_PROVIDER (adapter OpenAI). gpt-6-luna on chat.completions."""
    spec = next(s for s in models.MODEL_TABLE if s.provider == OPENAI_PROVIDER)
    return OpenAICompatibleAdapter(
        keystore,
        provider=OPENAI_PROVIDER,
        model_id=spec.model_id,
        base_url=OPENAI_BASE_URL,
        token_param="max_completion_tokens",
        # With reasoning on, temperature/top_p/logprobs must not be sent.
        extra_params={"reasoning_effort": "low"},
        sdk=sdk,
    )


def make_oci_adapter(
    keystore: KeyStore, region: str | None, sdk: ModuleType | None = None
) -> OpenAICompatibleAdapter:
    """Stato: CALL_PROVIDER (adapter OCI). OCI Generative AI service key on the
    regional OpenAI-compatible endpoint. An unsupported or missing region does
    not raise here: the adapter just reports not configured."""
    spec = next(s for s in models.MODEL_TABLE if s.provider == OCI_PROVIDER)
    return OpenAICompatibleAdapter(
        keystore,
        provider=OCI_PROVIDER,
        model_id=spec.model_id,
        # Oracle's docs do not name the output-limit field; max_tokens was
        # verified on a real tenancy (eu-frankfurt-1, 17 Oct 2026), and the
        # usage comes back as with OpenAI ...
        token_param="max_tokens",
        # ... nor a reasoning parameter: none is sent, and with none the
        # model left room for the answer (857 output tokens on 2,000 words).
        extra_params={},
        base_url=oci_base_url(region),
        requires_base_url=True,
        verify_with_ping=True,
        sdk=sdk,
    )
