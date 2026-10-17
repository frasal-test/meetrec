"""Fakes and builders shared by the tests/test_llm_*.py files.

Everything here is fake: no network, no provider SDK, no real key.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from meeting_recorder.config import SummaryConfig
from meeting_recorder.llm.adapters import AdapterRegistry
from meeting_recorder.llm.flow import run_llm_flow
from meeting_recorder.llm.errors import AuthError
from meeting_recorder.llm.keys import InMemoryKeyStore
from meeting_recorder.llm.models import MODEL_TABLE, ModelSpec, compute_cost
from meeting_recorder.llm.outcomes import Outcome
from meeting_recorder.llm.transcript import TranscriptInput
from meeting_recorder.llm.types import LLMRequest, LLMResponse, Usage
from meeting_recorder.llm.ui import (
    Choice,
    ChoiceScreen,
    RetryScreen,
)


FAKE_KEY = "sk-FAKE-KEY-DO-NOT-LEAK-123"
ALL_PROVIDERS = tuple(spec.provider for spec in MODEL_TABLE)


def make_transcript_text(words: int, word: str = "parola") -> str:
    """Speaker lines with exactly `words` words of speech (prefixes excluded)."""
    lines = []
    remaining = words
    index = 0
    while remaining > 0:
        count = min(10, remaining)
        speaker = "ME" if index % 2 == 0 else "REMOTE_01"
        lines.append(
            f"[00:00:{index % 60:02d}.000 - 00:00:{index % 60:02d}.900] "
            f"{speaker}: " + " ".join([word] * count)
        )
        remaining -= count
        index += 1
    return "\n".join(lines)


def ok(
    status: str = "complete",
    usage: Usage | None = None,
    text: str = "## Sintesi\nRiassunto di prova.",
) -> Callable[[LLMRequest], LLMResponse]:
    """A scripted success whose cost is computed from the model table."""
    used = usage or Usage(1000, 500)

    def make(req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=text,
            status=status,  # type: ignore[arg-type]
            usage=used,
            cost=compute_cost(req.model_id, used),
            latency_ms=12,
        )

    return make


class FakeAdapter:
    """Reads only the key store, like the real adapters must (T47). Each call
    to complete consumes the next scripted item: an exception instance is
    raised, a callable is called with the request; an empty script succeeds."""

    def __init__(
        self,
        provider: str,
        keystore: InMemoryKeyStore,
        script: list[Any] | None = None,
    ) -> None:
        self.provider = provider
        self.keystore = keystore
        self.script = list(script or [])
        self.requests: list[LLMRequest] = []
        self.is_configured_calls = 0

    def is_configured(self) -> bool:
        self.is_configured_calls += 1
        return self.keystore.has_key(self.provider)

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        # Like the real adapters: the key is read at call time, never before.
        if not self.keystore.get_key(self.provider):
            raise AuthError()
        item = self.script.pop(0) if self.script else ok()
        if isinstance(item, BaseException):
            raise item
        return item(req) if callable(item) else item


class FakeUI:
    """Records every call and answers from a script. A missing answer fails
    the test: it means the flow asked something it should not have."""

    def __init__(
        self,
        choices: list[Choice] | None = None,
        retries: list[bool] | None = None,
    ) -> None:
        self.choices = list(choices or [])
        self.retries = list(retries or [])
        self.choice_screens: list[ChoiceScreen] = []
        self.retry_screens: list[RetryScreen] = []
        self.progress_calls: list[str] = []
        self.outcomes: list[Outcome] = []

    def ask_choice(self, screen: ChoiceScreen) -> Choice:
        self.choice_screens.append(screen)
        if not self.choices:
            raise AssertionError("unexpected ASK_CHOICE")
        return self.choices.pop(0)

    def ask_retry(self, screen: RetryScreen) -> bool:
        self.retry_screens.append(screen)
        if not self.retries:
            raise AssertionError("unexpected ASK_RETRY")
        return self.retries.pop(0)

    def progress(self, model_label: str) -> None:
        self.progress_calls.append(model_label)

    def outcome(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)


@dataclass
class Harness:
    """A session directory, a key store with the given providers, fake adapters
    and a UI, ready to call run_llm_flow."""

    root: Path
    providers: tuple[str, ...] = ("anthropic",)
    words: int = 200
    language: str | None = "it"
    automatic: bool = False
    default_model: str | None = None
    models: tuple[ModelSpec, ...] = MODEL_TABLE
    text: str | None = None
    scripts: dict[str, list[Any]] = field(default_factory=dict)
    choices: list[Choice] = field(default_factory=list)
    retries: list[bool] = field(default_factory=list)
    keystore: InMemoryKeyStore = field(default_factory=InMemoryKeyStore)
    log_records: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.session_dir = self.root / "session"
        self.emergency_dir = self.root / "emergency"
        (self.session_dir / "transcripts").mkdir(parents=True)
        for provider in self.providers:
            self.keystore[provider] = FAKE_KEY
        self.adapters = {
            spec.provider: FakeAdapter(
                spec.provider, self.keystore, self.scripts.get(spec.provider)
            )
            for spec in self.models
        }
        self.registry = AdapterRegistry()
        for provider, adapter in self.adapters.items():
            self.registry.register(provider, lambda _store, a=adapter: a)
        self.ui = FakeUI(self.choices, self.retries)
        text = self.text if self.text is not None else make_transcript_text(self.words)
        path = self.session_dir / "transcripts" / "transcript.speakers.txt"
        path.write_text(text + "\n", encoding="utf-8")
        self.transcript = TranscriptInput(
            transcript_id=self.session_dir.name,
            path=path,
            text=text,
            language=self.language,
        )

    def run(self, *, memory_log: bool = True) -> Outcome:
        return run_llm_flow(
            self.transcript,
            summary_config=SummaryConfig(
                automatic=self.automatic, default_model=self.default_model
            ),
            registry=self.registry,
            keystore=self.keystore,
            ui=self.ui,
            session_dir=self.session_dir,
            emergency_dir=self.emergency_dir,
            log_sink=self.log_records.append if memory_log else None,
            models=self.models,
        )

    @property
    def transitions(self) -> list[tuple[str, str]]:
        return [
            (r["from"], r["to"]) for r in self.log_records if "from" in r
        ]

    def all_adapter_requests(self) -> list[LLMRequest]:
        return [r for a in self.adapters.values() for r in a.requests]

    def all_text_on_disk(self) -> str:
        """Every file written under the temp root, concatenated."""
        parts = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    def saved_records(self) -> list[dict[str, Any]]:
        folder = self.session_dir / "summaries"
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(folder.glob("*.json"))
        ]


def temp_root() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()


CHOOSE_ANTHROPIC_IT = Choice("claude-haiku-5-5", "it")
CHOOSE_NONE = Choice(None, None)
