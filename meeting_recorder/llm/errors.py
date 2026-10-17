"""Normalized provider errors (docs/flow/adapter-contract.md).

Messages are for the user: no technical detail, never an API key.
"""
from __future__ import annotations

from .types import Cost, Usage


class LLMError(Exception):
    retryable = False
    default_message = "Error while processing."

    def __init__(
        self,
        user_message: str | None = None,
        *,
        usage: Usage | None = None,
        cost: Cost | None = None,
    ) -> None:
        self.user_message = user_message or self.default_message
        self.usage = usage
        self.cost = cost
        super().__init__(self.user_message)


class TimeoutLLMError(LLMError):
    retryable = True
    default_message = "The service did not respond in time."


class RateLimitError(LLMError):
    retryable = True
    default_message = "Too many requests to the service right now."


class ProviderError(LLMError):
    retryable = True
    default_message = "The service is temporarily unavailable."


class InsufficientCreditsError(LLMError):
    default_message = "No credit left with the service."


class AuthError(LLMError):
    default_message = "The API key is no longer valid."


class ModelNotFoundError(LLMError):
    default_message = "The model is no longer available."


class BadRequestError(LLMError):
    default_message = "The service rejected the request."


class ContentFilteredError(LLMError):
    default_message = "The service blocked the response."


class InvalidResponseError(LLMError):
    default_message = "The service returned an invalid response."


class UnknownLLMError(LLMError):
    default_message = "Unexpected error while processing."
