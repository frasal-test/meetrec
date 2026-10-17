"""Distinct end results of the flow (I16) and the messages for the user (docs/flow/ui.md).

Messages carry no raw technical detail and never an API key.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any


class OutcomeKind(str, Enum):
    DONE = "DONE"
    END_NO_LLM = "END_NO_LLM"
    END_ERROR = "END_ERROR"
    END_SAVE_ERROR = "END_SAVE_ERROR"
    END_FATAL = "END_FATAL"


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    # too_short | no_keys | too_long | declined for END_NO_LLM; the error
    # class name for END_ERROR; the cause for END_FATAL.
    reason: str | None
    # complete | truncated, only for DONE.
    status: str | None
    message: str
    result_path: Path | None = None
    emergency_path: Path | None = None
    run_id: str | None = None
    # Tokens and cost of every provider call (I11), so that a run that ended
    # in an error still reports what it cost. Empty if no call was made.
    attempts: tuple[dict[str, Any], ...] = ()
    total_cost: Decimal | None = None


PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "oci": "Oracle OCI",
}


def provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider)


def format_real_cost(amount: Decimal | None) -> str:
    """The cost actually incurred. Unknown stays unknown, never $0."""
    if amount is None:
        return "cost unavailable"
    shown = amount.quantize(Decimal("0.0001"), rounding=ROUND_UP)
    return f"${shown}"


def outcome_to_dict(outcome: Outcome) -> dict[str, Any]:
    return {
        "kind": outcome.kind.value,
        "reason": outcome.reason,
        "status": outcome.status,
        "message": outcome.message,
        "result_path": str(outcome.result_path) if outcome.result_path else None,
        "emergency_path": (
            str(outcome.emergency_path) if outcome.emergency_path else None
        ),
        "run_id": outcome.run_id,
        "attempts": list(outcome.attempts),
        "total_cost": (
            str(outcome.total_cost) if outcome.total_cost is not None else None
        ),
    }


def done(
    *,
    run_id: str,
    result_path: Path,
    model_label: str,
    total_cost: Decimal | None,
    status: str,
) -> Outcome:
    message = (
        f"Summary saved to {result_path} · {model_label} · "
        f"{format_real_cost(total_cost)}"
    )
    if status == "truncated":
        message += ". The response was cut short by the length limit"
    return Outcome(
        OutcomeKind.DONE, None, status, message,
        result_path=result_path, run_id=run_id,
    )


def no_llm(
    reason: str,
    *,
    run_id: str,
    transcript_path: Path,
    model_label: str | None = None,
) -> Outcome:
    if reason == "too_short":
        message = (
            f"Transcript too short for a summary, saved to {transcript_path}"
        )
    elif reason == "no_keys":
        message = "Transcript saved. No LLM configured"
    elif reason == "too_long":
        target = model_label or "a summary (over ~5 hours)"
        message = f"Transcript too long for {target}. Transcript saved"
    elif reason == "declined":
        message = "Transcript saved"
    else:
        raise ValueError(f"Unknown END_NO_LLM reason: {reason!r}")
    return Outcome(OutcomeKind.END_NO_LLM, reason, None, message, run_id=run_id)


def error(
    error_name: str, *, run_id: str, provider: str
) -> Outcome:
    service = provider_name(provider)
    messages = {
        "InsufficientCreditsError": f"No credit left on {service}. Transcript saved",
        "AuthError": f"Could not use the {service} API key: check it in Settings. Transcript saved",
        "ModelNotFoundError": (
            f"Model no longer available from {service}: update the app. "
            "Transcript saved"
        ),
        "ContentFilteredError": "The provider blocked the response. Transcript saved",
    }
    message = messages.get(
        error_name,
        f"Error while processing ({error_name}). Transcript saved",
    )
    return Outcome(OutcomeKind.END_ERROR, error_name, None, message, run_id=run_id)


def save_error(*, run_id: str, emergency_path: Path | None) -> Outcome:
    if emergency_path is None:
        message = (
            "Summary received but not saved: the emergency file could not "
            "be written either"
        )
    else:
        message = f"Response saved as an emergency copy in {emergency_path}"
    return Outcome(
        OutcomeKind.END_SAVE_ERROR, "save_failed", None, message,
        emergency_path=emergency_path, run_id=run_id,
    )


def fatal(reason: str, *, run_id: str | None = None) -> Outcome:
    """END_FATAL. Produced by the transcription side (SAVE_TRANSCRIPT), before
    this flow starts; kept here so the five outcomes live in one place."""
    return Outcome(
        OutcomeKind.END_FATAL, reason, None,
        f"Could not save the transcript ({reason})", run_id=run_id,
    )
