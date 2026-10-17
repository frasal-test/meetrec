"""The LLM flow from CHECK_LENGTH to the end (docs/flow/flow.md).

TRANSCRIPT_READY and SAVE_TRANSCRIPT run in the transcription worker, which
ends there; this flow receives a transcript that is already saved. One
function per state; each docstring names its state or transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import SummaryConfig
from ..sessions import utc_now
from . import outcomes
from .adapters import AdapterRegistry, ProviderAdapter
from .errors import LLMError, UnknownLLMError
from .keys import KeyStore
from .log import FlowLog, file_sink
from .models import (
    CURRENCY,
    MODEL_TABLE,
    ModelSpec,
    compute_cost,
    estimate_max_cost,
    fits,
    find_model,
)
from .outcomes import Outcome
from .prompts import build_request, estimate_prompt_tokens
from .storage import emergency_save, new_run_id, render_markdown, save_result
from .transcript import TranscriptInput, count_words
from .types import Cost, LLMRequest, LLMResponse, Usage
from .ui import (
    TOO_LONG_REASON,
    Choice,
    ChoiceScreen,
    ModelOption,
    ProtocolError,
    RetryScreen,
    UserInterface,
    format_max_cost,
    language_label,
    validate_choice,
)


# Below this many words the transcript is read before it is summarized (I3).
MIN_WORDS = 150
# 1 call + 1 "Riprova" chosen by the user (I10).
MAX_ATTEMPTS = 2

CHECK_LENGTH = "CHECK_LENGTH"
CHECK_KEYS = "CHECK_KEYS"
ASK_CHOICE = "ASK_CHOICE"
BUILD_PROMPT = "BUILD_PROMPT"
CALL_PROVIDER = "CALL_PROVIDER"
ASK_RETRY = "ASK_RETRY"
SAVE_RESULT = "SAVE_RESULT"
EMERGENCY_SAVE = "EMERGENCY_SAVE"
DONE = "DONE"
END_NO_LLM = "END_NO_LLM"
END_ERROR = "END_ERROR"
END_SAVE_ERROR = "END_SAVE_ERROR"

MODE_MANUAL = "manual"
MODE_AUTOMATIC = "automatic"
AUTOMATIC_RESPONSE_LANGUAGE = "auto"


@dataclass
class _Run:
    run_id: str
    transcript: TranscriptInput
    session_dir: Path
    emergency_dir: Path
    log: FlowLog
    clock: Callable[[], str]
    models: Sequence[ModelSpec]
    templates_dir: Path | None
    adapters: dict[str, ProviderAdapter] = field(default_factory=dict)


@dataclass(frozen=True)
class _Candidate:
    """A model with a usable key, and whether the transcript fits its context."""

    spec: ModelSpec
    fits: bool


@dataclass(frozen=True)
class _Selection:
    spec: ModelSpec
    response_language: str
    mode: str


@dataclass(frozen=True)
class _KeyCheck:
    outcome: Outcome | None = None
    selection: _Selection | None = None
    candidates: tuple[_Candidate, ...] = ()


def run_llm_flow(
    transcript: TranscriptInput,
    *,
    summary_config: SummaryConfig,
    registry: AdapterRegistry,
    keystore: KeyStore,
    ui: UserInterface,
    session_dir: Path,
    emergency_dir: Path,
    log_sink: Callable[[dict[str, Any]], None] | None = None,
    clock: Callable[[], str] | None = None,
    models: Sequence[ModelSpec] = MODEL_TABLE,
    templates_dir: Path | None = None,
) -> Outcome:
    """From CHECK_LENGTH to one of the end states. Always returns an Outcome
    (I16) and passes it to ui.outcome. `models` and `templates_dir` are
    injectable for tests."""
    run_id = new_run_id()
    run = _Run(
        run_id=run_id,
        transcript=transcript,
        session_dir=session_dir,
        emergency_dir=emergency_dir,
        log=FlowLog(run_id, log_sink or file_sink(session_dir), clock),
        clock=clock or utc_now,
        models=models,
        templates_dir=templates_dir,
    )
    result = _run(run, summary_config, registry, keystore, ui)
    ui.outcome(result)
    return result


def _run(
    run: _Run,
    summary_config: SummaryConfig,
    registry: AdapterRegistry,
    keystore: KeyStore,
    ui: UserInterface,
) -> Outcome:
    words = check_length(run)
    if isinstance(words, Outcome):
        return words

    checked = check_keys(run, summary_config, registry, keystore)
    if checked.outcome is not None:
        return checked.outcome
    selection = checked.selection
    if selection is None:
        asked = ask_choice(run, checked.candidates, words, ui)
        if isinstance(asked, Outcome):
            return asked
        selection = asked

    request = build_prompt(run, selection)
    attempts: list[dict[str, Any]] = []
    response = call_with_retry(run, selection, request, attempts, ui)
    if isinstance(response, Outcome):
        return response

    record = _result_record(run, selection, request, response, attempts)
    saved = save_result_state(run, selection, record)
    if saved is None:
        return emergency_save_state(run, record)
    run.log.transition(SAVE_RESULT, DONE, "saved")
    return replace(
        outcomes.done(
            run_id=run.run_id,
            result_path=saved,
            model_label=selection.spec.label,
            total_cost=_total_cost(attempts),
            status=response.status,
        ),
        attempts=tuple(attempts),
        total_cost=_total_cost(attempts),
    )


def check_length(run: _Run) -> int | Outcome:
    """Stato: CHECK_LENGTH. Transizioni: CHECK_LENGTH -> END_NO_LLM (too_short)
    o CHECK_LENGTH -> CHECK_KEYS. Returns the word count to go on."""
    words = count_words(run.transcript.text)
    if words < MIN_WORDS:
        run.log.transition(CHECK_LENGTH, END_NO_LLM, "too_short")
        return outcomes.no_llm(
            "too_short",
            run_id=run.run_id,
            transcript_path=run.transcript.path,
        )
    run.log.transition(CHECK_LENGTH, CHECK_KEYS, "words_at_or_above_threshold")
    return words


def _adapter_for(
    run: _Run, provider: str, registry: AdapterRegistry, keystore: KeyStore
) -> ProviderAdapter | None:
    if provider not in run.adapters:
        try:
            adapter = registry.create(provider, keystore)
        except Exception:
            run.log.note(f"adapter for {provider} could not be created")
            return None
        if adapter is None:
            return None
        run.adapters[provider] = adapter
    return run.adapters[provider]


def _has_key(
    run: _Run, provider: str, registry: AdapterRegistry, keystore: KeyStore
) -> bool:
    """I4: a registered adapter that reports a configured key."""
    adapter = _adapter_for(run, provider, registry, keystore)
    if adapter is None:
        return False
    try:
        return bool(adapter.is_configured())
    except Exception:
        run.log.note(f"is_configured failed for {provider}")
        return False


def check_keys(
    run: _Run,
    summary_config: SummaryConfig,
    registry: AdapterRegistry,
    keystore: KeyStore,
) -> _KeyCheck:
    """Stato: CHECK_KEYS. Decides among END_NO_LLM (no_keys / too_long),
    BUILD_PROMPT (automatic mode) and ASK_CHOICE (manual mode, or automatic
    with an unusable default)."""
    tokens = estimate_prompt_tokens(
        run.transcript.text, run.transcript.language, run.templates_dir
    )
    candidates = tuple(
        _Candidate(spec, fits(spec, tokens))
        for spec in run.models
        if _has_key(run, spec.provider, registry, keystore)
    )

    if not candidates:
        run.log.transition(CHECK_KEYS, END_NO_LLM, "no_keys")
        return _KeyCheck(
            outcome=outcomes.no_llm(
                "no_keys",
                run_id=run.run_id,
                transcript_path=run.transcript.path,
            )
        )
    if not any(c.fits for c in candidates):
        run.log.transition(CHECK_KEYS, END_NO_LLM, "too_long")
        return _KeyCheck(
            outcome=outcomes.no_llm(
                "too_long",
                run_id=run.run_id,
                transcript_path=run.transcript.path,
            )
        )

    if summary_config.automatic:
        default = next(
            (
                c
                for c in candidates
                if c.spec.model_id == summary_config.default_model
            ),
            None,
        )
        if default is not None and default.fits:
            run.log.transition(
                CHECK_KEYS, BUILD_PROMPT, f"automatic:{default.spec.model_id}"
            )
            return _KeyCheck(
                selection=_Selection(
                    default.spec, AUTOMATIC_RESPONSE_LANGUAGE, MODE_AUTOMATIC
                )
            )
        if default is not None:
            run.log.transition(
                CHECK_KEYS, END_NO_LLM, "too_long:automatic_default"
            )
            return _KeyCheck(
                outcome=outcomes.no_llm(
                    "too_long",
                    run_id=run.run_id,
                    transcript_path=run.transcript.path,
                    model_label=default.spec.label,
                )
            )
        run.log.note(
            "automatic mode: the default model has no key or is unknown; "
            "asking the user"
        )
        run.log.transition(
            CHECK_KEYS, ASK_CHOICE, "automatic_default_unavailable"
        )
        return _KeyCheck(candidates=candidates)

    run.log.transition(CHECK_KEYS, ASK_CHOICE, "manual")
    return _KeyCheck(candidates=candidates)


def build_choice_screen(
    run: _Run, candidates: Sequence[_Candidate], words: int
) -> ChoiceScreen:
    """Stato: ASK_CHOICE (the screen). Only models with a key, in table order;
    too-long ones disabled. No option, and no language, is preselected (I5)."""
    tokens = estimate_prompt_tokens(
        run.transcript.text, run.transcript.language, run.templates_dir
    )
    options = tuple(
        ModelOption(
            model_id=c.spec.model_id,
            label=c.spec.label,
            max_cost=format_max_cost(estimate_max_cost(c.spec, tokens)),
            enabled=True,
        )
        if c.fits
        else ModelOption(
            model_id=c.spec.model_id,
            label=c.spec.label,
            max_cost=None,
            enabled=False,
            disabled_reason=TOO_LONG_REASON,
        )
        for c in candidates
    )
    return ChoiceScreen(
        transcript_name=run.transcript.transcript_id,
        language_label=language_label(run.transcript.language),
        word_count=words,
        options=options,
    )


def ask_choice(
    run: _Run,
    candidates: Sequence[_Candidate],
    words: int,
    ui: UserInterface,
) -> _Selection | Outcome:
    """Stato: ASK_CHOICE. Transizioni: ASK_CHOICE -> END_NO_LLM (declined)
    o ASK_CHOICE -> BUILD_PROMPT. An invalid answer raises ProtocolError."""
    screen = build_choice_screen(run, candidates, words)
    choice: Choice = ui.ask_choice(screen)
    validate_choice(screen, choice)
    if choice.model_id is None:
        run.log.transition(ASK_CHOICE, END_NO_LLM, "declined")
        return outcomes.no_llm(
            "declined", run_id=run.run_id, transcript_path=run.transcript.path
        )
    spec = next(c.spec for c in candidates if c.spec.model_id == choice.model_id)
    assert choice.response_language is not None  # checked by validate_choice
    run.log.transition(ASK_CHOICE, BUILD_PROMPT, f"choice:{spec.model_id}")
    return _Selection(spec, choice.response_language, MODE_MANUAL)


def build_prompt(run: _Run, selection: _Selection) -> LLMRequest:
    """Stato: BUILD_PROMPT. Transizione: BUILD_PROMPT -> CALL_PROVIDER."""
    request = build_request(
        run_id=run.run_id,
        spec=selection.spec,
        transcript_text=run.transcript.text,
        transcript_language=run.transcript.language,
        response_language=selection.response_language,
        templates_dir=run.templates_dir,
        log=run.log.note,
    )
    run.log.transition(BUILD_PROMPT, CALL_PROVIDER, request.template_id)
    return request


def _attempt_cost(
    run: _Run,
    model_id: str,
    usage: Usage | None,
    reported: Cost | None,
) -> Cost:
    """I11: the cost the adapter reported, else computed from the usage, else
    unknown (None, never 0)."""
    if reported is not None and reported.amount is not None:
        return reported
    if usage is not None:
        return compute_cost(model_id, usage, run.models)
    return Cost(amount=None, currency=CURRENCY)


def _attempt_record(
    run: _Run,
    number: int,
    model_id: str,
    usage: Usage | None,
    cost: Cost,
    *,
    response: LLMResponse | None,
    error: LLMError | None,
) -> dict[str, Any]:
    return {
        "attempt": number,
        "ok": error is None,
        "status": response.status if response else None,
        "error": type(error).__name__ if error else None,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "cost": str(cost.amount) if cost.amount is not None else None,
        "currency": cost.currency,
        "latency_ms": response.latency_ms if response else None,
        "finished_at": run.clock(),
    }


def call_provider(
    run: _Run,
    adapter: ProviderAdapter,
    request: LLMRequest,
    number: int,
    attempts: list[dict[str, Any]],
) -> LLMResponse | LLMError:
    """Stato: CALL_PROVIDER. One call through the adapter; records the attempt
    and its cost (I11). Any exception that is not an LLMError is wrapped, so
    the flow never crashes on an adapter. A ProtocolError (the app answered
    the key request with something invalid) is not the provider's doing and
    goes up to llm-run, like every other invalid answer."""
    try:
        response = adapter.complete(request)
    except LLMError as exc:
        error: LLMError = exc
    except ProtocolError:
        raise
    except Exception:
        # Only the class is kept: the original message could hold anything.
        error = UnknownLLMError()
    else:
        cost = _attempt_cost(run, request.model_id, response.usage, response.cost)
        record = _attempt_record(
            run, number, request.model_id, response.usage, cost,
            response=response, error=None,
        )
        attempts.append(record)
        run.log.attempt(record)
        return response

    cost = _attempt_cost(run, request.model_id, error.usage, error.cost)
    record = _attempt_record(
        run, number, request.model_id, error.usage, cost,
        response=None, error=error,
    )
    attempts.append(record)
    run.log.attempt(record)
    return error


def ask_retry(run: _Run, error: LLMError, ui: UserInterface) -> bool:
    """Stato: ASK_RETRY. Transizioni: ASK_RETRY -> CALL_PROVIDER (Riprova)
    o ASK_RETRY -> END_ERROR (Esci). No default answer."""
    retry = bool(ui.ask_retry(RetryScreen(message=error.user_message)))
    if retry:
        run.log.transition(ASK_RETRY, CALL_PROVIDER, "retry")
    else:
        run.log.transition(ASK_RETRY, END_ERROR, "exit")
    return retry


def call_with_retry(
    run: _Run,
    selection: _Selection,
    request: LLMRequest,
    attempts: list[dict[str, Any]],
    ui: UserInterface,
) -> LLMResponse | Outcome:
    """Transizioni di CALL_PROVIDER: successo -> SAVE_RESULT; errore ritentabile
    al primo tentativo -> ASK_RETRY; errore non ritentabile o secondo tentativo
    -> END_ERROR. Never a third attempt, never an automatic retry (I10): the
    second call exists only after ASK_RETRY returned Riprova, with the same
    request object."""
    adapter = run.adapters[selection.spec.provider]
    number = 1
    while True:
        ui.progress(selection.spec.label)
        result = call_provider(run, adapter, request, number, attempts)
        if isinstance(result, LLMResponse):
            run.log.transition(
                CALL_PROVIDER, SAVE_RESULT, f"success:{result.status}"
            )
            return result
        name = type(result).__name__
        if result.retryable and number < MAX_ATTEMPTS:
            run.log.transition(CALL_PROVIDER, ASK_RETRY, name)
            if ask_retry(run, result, ui):
                number += 1
                continue
            return _error_outcome(run, selection, name, attempts)
        run.log.transition(CALL_PROVIDER, END_ERROR, name)
        return _error_outcome(run, selection, name, attempts)


def _error_outcome(
    run: _Run,
    selection: _Selection,
    name: str,
    attempts: Sequence[dict[str, Any]],
) -> Outcome:
    return replace(
        outcomes.error(name, run_id=run.run_id, provider=selection.spec.provider),
        attempts=tuple(attempts),
        total_cost=_total_cost(attempts),
    )


def _total_cost(attempts: Sequence[dict[str, Any]]) -> Decimal | None:
    """Sum of the known amounts; None when no attempt has a known cost."""
    known = [Decimal(a["cost"]) for a in attempts if a["cost"] is not None]
    return sum(known, Decimal(0)) if known else None


def _result_record(
    run: _Run,
    selection: _Selection,
    request: LLMRequest,
    response: LLMResponse,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    """The I14 fields."""
    total = _total_cost(attempts)
    return {
        "run_id": run.run_id,
        "transcript_id": run.transcript.transcript_id,
        "provider": selection.spec.provider,
        "model": selection.spec.model_id,
        "mode": selection.mode,
        "template_id": request.template_id,
        "template_language": request.template_language,
        "response_language": request.response_language,
        "text": response.text,
        "status": response.status,
        "attempts": attempts,
        "total_cost": str(total) if total is not None else None,
        "currency": selection.spec.currency,
        "created_at": run.clock(),
    }


def save_result_state(
    run: _Run, selection: _Selection, record: dict[str, Any]
) -> Path | None:
    """Stato: SAVE_RESULT. Returns the saved path, or None after logging the
    transition SAVE_RESULT -> EMERGENCY_SAVE."""
    markdown = render_markdown(record, selection.spec.label)
    try:
        return save_result(run.session_dir, run.run_id, record, markdown)
    except Exception as exc:
        run.log.transition(SAVE_RESULT, EMERGENCY_SAVE, type(exc).__name__)
        return None


def emergency_save_state(run: _Run, record: dict[str, Any]) -> Outcome:
    """Stato: EMERGENCY_SAVE. Transizione: EMERGENCY_SAVE -> END_SAVE_ERROR
    (always, I13). If even the emergency file cannot be written the outcome
    says so, without a path."""
    try:
        path: Path | None = emergency_save(record, run.emergency_dir)
        reason = "emergency_saved"
    except Exception as exc:
        path = None
        reason = f"emergency_failed:{type(exc).__name__}"
    run.log.transition(EMERGENCY_SAVE, END_SAVE_ERROR, reason)
    return replace(
        outcomes.save_error(run_id=run.run_id, emergency_path=path),
        attempts=tuple(record["attempts"]),
        total_cost=_total_cost(record["attempts"]),
    )
