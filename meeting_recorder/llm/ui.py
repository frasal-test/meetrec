"""The two choice screens of the flow and the JSON-lines bridge to the app (docs/flow/ui.md).

Nothing here carries a preselected value (I5): not for the model, not for the
language, not for Riprova/Esci.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import IO, Any, Protocol

from .outcomes import Outcome, outcome_to_dict


# (code used in the flow, label shown on the button)
RESPONSE_LANGUAGE_CHOICES = (
    ("it", "Ita"),
    ("en", "Eng"),
    ("es", "Esp"),
    ("auto", "Auto"),
)
RESPONSE_LANGUAGE_CODES = tuple(code for code, _ in RESPONSE_LANGUAGE_CHOICES)

TOO_LONG_REASON = "transcript troppo lungo"
NONE_OPTION_LABEL = "Nessuno"
NONE_OPTION_HINT = "solo transcript"

_LANGUAGE_LABELS = {
    "it": "italiano",
    "en": "inglese",
    "es": "spagnolo",
    "auto": "rilevamento automatico",
}


class ProtocolError(Exception):
    """The app answered something that is not a valid answer. Never guessed."""


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    label: str
    # "max ~$0.09"; None when the option is disabled.
    max_cost: str | None
    enabled: bool
    disabled_reason: str | None = None


@dataclass(frozen=True)
class ChoiceScreen:
    transcript_name: str
    # "rilevamento automatico" for auto.
    language_label: str
    word_count: int
    options: tuple[ModelOption, ...]
    response_languages: tuple[tuple[str, str], ...] = RESPONSE_LANGUAGE_CHOICES


@dataclass(frozen=True)
class RetryScreen:
    message: str


@dataclass(frozen=True)
class Choice:
    # None = "Nessuno".
    model_id: str | None
    response_language: str | None


class UserInterface(Protocol):
    def ask_choice(self, screen: ChoiceScreen) -> Choice: ...

    def ask_retry(self, screen: RetryScreen) -> bool:
        """True = Riprova, False = Esci."""

    def progress(self, model_label: str) -> None: ...

    def outcome(self, outcome: Outcome) -> None: ...


def language_label(language: str | None) -> str:
    return _LANGUAGE_LABELS.get(language or "", "lingua non indicata")


def format_max_cost(amount: Decimal) -> str:
    """"max ~$0.09". Rounded up to the cent: it is an upper bound."""
    shown = max(amount.quantize(Decimal("0.01"), rounding=ROUND_UP), Decimal("0.01"))
    return f"max ~${shown}"


def validate_choice(screen: ChoiceScreen, choice: Choice) -> None:
    """Raises ProtocolError for an answer the screen could not have produced."""
    if choice.model_id is None:
        return
    option = next(
        (o for o in screen.options if o.model_id == choice.model_id), None
    )
    if option is None:
        raise ProtocolError("unknown model in the answer")
    if not option.enabled:
        raise ProtocolError("disabled model in the answer")
    if choice.response_language not in RESPONSE_LANGUAGE_CODES:
        raise ProtocolError("missing or unknown response language in the answer")


def _screen_to_dict(screen: ChoiceScreen) -> dict[str, Any]:
    return {
        "transcript_name": screen.transcript_name,
        "language": screen.language_label,
        "word_count": screen.word_count,
        "none_option": {"label": NONE_OPTION_LABEL, "hint": NONE_OPTION_HINT},
        "options": [
            {
                "model_id": o.model_id,
                "label": o.label,
                "max_cost": o.max_cost,
                "enabled": o.enabled,
                "disabled_reason": o.disabled_reason,
            }
            for o in screen.options
        ],
        "response_languages": [
            {"code": code, "label": label}
            for code, label in screen.response_languages
        ],
    }


class JsonLinesUI:
    """Speaks to the app: one JSON event per line on stdout, one JSON answer
    line on stdin for the two asks (flow.md, "Confine tra processi")."""

    def __init__(self, stdin: IO[str], stdout: IO[str]) -> None:
        self._stdin = stdin
        self._stdout = stdout

    def _emit(self, event: dict[str, Any]) -> None:
        self._stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._stdout.flush()

    def _read_answer(self) -> dict[str, Any]:
        line = self._stdin.readline()
        if not line:
            raise ProtocolError("input closed before the answer")
        try:
            answer = json.loads(line)
        except ValueError:
            raise ProtocolError("answer is not valid JSON") from None
        if not isinstance(answer, dict):
            raise ProtocolError("answer is not a JSON object")
        return answer

    def ask_choice(self, screen: ChoiceScreen) -> Choice:
        """Stato: ASK_CHOICE."""
        self._emit({"event": "ask_choice", "screen": _screen_to_dict(screen)})
        answer = self._read_answer()
        if "model_id" not in answer:
            raise ProtocolError("answer has no model_id")
        model_id = answer["model_id"]
        language = answer.get("response_language")
        if model_id is not None and not isinstance(model_id, str):
            raise ProtocolError("model_id must be a string or null")
        if language is not None and not isinstance(language, str):
            raise ProtocolError("response_language must be a string or null")
        choice = Choice(model_id=model_id, response_language=language)
        validate_choice(screen, choice)
        return choice

    def ask_retry(self, screen: RetryScreen) -> bool:
        """Stato: ASK_RETRY. True = Riprova."""
        self._emit({"event": "ask_retry", "message": screen.message})
        answer = self._read_answer()
        retry = answer.get("retry")
        if not isinstance(retry, bool):
            raise ProtocolError("answer has no boolean retry")
        return retry

    def progress(self, model_label: str) -> None:
        """Stato: CALL_PROVIDER (waiting indicator)."""
        self._emit({"event": "progress", "model": model_label})

    def outcome(self, outcome: Outcome) -> None:
        self._emit({"event": "outcome", **outcome_to_dict(outcome)})
