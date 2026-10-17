"""Provider-neutral request and response types (docs/flow/adapter-contract.md)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class LLMRequest:
    run_id: str
    provider: str
    model_id: str
    # Compiled template + response-language instruction (I7) + delimiter rule (I8).
    system_prompt: str
    # Already wrapped between delimiters by BUILD_PROMPT, never bare (I8).
    transcript: str
    template_id: str
    template_language: str  # it | en | es
    response_language: str  # it | en | es | auto
    max_output_tokens: int


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class Cost:
    # None when it cannot be computed. Never 0 as a fallback (I11).
    amount: Decimal | None
    currency: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    status: Literal["complete", "truncated"]
    usage: Usage
    cost: Cost
    latency_ms: int
