"""Normalized provider errors (docs/flow/adapter-contract.md).

Messages are for the user: no technical detail, never an API key.
"""
from __future__ import annotations

from .types import Cost, Usage


class LLMError(Exception):
    retryable = False
    default_message = "Errore durante l'elaborazione."

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
    default_message = "Il servizio non ha risposto in tempo."


class RateLimitError(LLMError):
    retryable = True
    default_message = "Troppe richieste al servizio in questo momento."


class ProviderError(LLMError):
    retryable = True
    default_message = "Il servizio è temporaneamente non disponibile."


class InsufficientCreditsError(LLMError):
    default_message = "Credito esaurito presso il servizio."


class AuthError(LLMError):
    default_message = "La API key non è più valida."


class ModelNotFoundError(LLMError):
    default_message = "Il modello non è più disponibile."


class BadRequestError(LLMError):
    default_message = "Il servizio ha rifiutato la richiesta."


class ContentFilteredError(LLMError):
    default_message = "Il servizio ha bloccato la risposta."


class InvalidResponseError(LLMError):
    default_message = "Il servizio ha restituito una risposta non valida."


class UnknownLLMError(LLMError):
    default_message = "Errore imprevisto durante l'elaborazione."
