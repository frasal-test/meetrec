"""Model table and the pure cost/context functions (docs/flow/adapter-contract.md)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .types import Cost, Usage


# One limit for every model, below every long-context price tier (I17), so the
# table can carry a single price per model. The tokens are estimated as
# characters / 4, so the limit keeps 10% below the lowest tier (Anthropic,
# 100,000) to absorb the estimate's error.
MAX_INPUT_TOKENS = 90_000

CURRENCY = "USD"
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model_id: str
    label: str
    price_input: Decimal  # per 1M input tokens
    price_output: Decimal  # per 1M output tokens
    currency: str
    max_output_tokens: int
    context_window: int  # prompt + response
    # Input tokens above which the provider charges more. None if it has none.
    long_context_threshold: int | None


MODEL_TABLE: tuple[ModelSpec, ...] = (
    ModelSpec(
        provider="anthropic",
        model_id="claude-haiku-5-5",
        label="Claude Haiku 5.5",
        price_input=Decimal("0.10"),
        price_output=Decimal("0.50"),
        currency=CURRENCY,
        max_output_tokens=16000,
        context_window=1_000_000,
        long_context_threshold=100_000,
    ),
    ModelSpec(
        provider="openai",
        model_id="gpt-6-luna",
        label="GPT-6 Luna",
        price_input=Decimal("0.10"),
        price_output=Decimal("0.50"),
        currency=CURRENCY,
        max_output_tokens=16000,
        # DA VERIFICARE: soglia long context e context_window di GPT-6 Luna
        context_window=1_000_000,
        long_context_threshold=None,
    ),
    ModelSpec(
        provider="oci",
        model_id="openai.gpt-oss-120b",
        label="gpt-oss-120b (OCI)",
        price_input=Decimal("0.15"),
        price_output=Decimal("0.60"),
        currency=CURRENCY,
        max_output_tokens=16000,
        context_window=128_000,
        long_context_threshold=None,
    ),
)


def find_model(
    model_id: str, table: Sequence[ModelSpec] = MODEL_TABLE
) -> ModelSpec | None:
    for spec in table:
        if spec.model_id == model_id:
            return spec
    return None


def estimate_tokens(text: str) -> int:
    """Rough token count: ceil(characters / 4)."""
    return math.ceil(len(text) / 4)


def compute_cost(
    model_id: str,
    usage: Usage,
    table: Sequence[ModelSpec] = MODEL_TABLE,
) -> Cost:
    """Real cost of one call. Missing tokens or unknown model give None, never 0 (I11)."""
    spec = find_model(model_id, table)
    if (
        spec is None
        or usage.input_tokens is None
        or usage.output_tokens is None
    ):
        return Cost(amount=None, currency=CURRENCY)
    amount = (
        Decimal(usage.input_tokens) * spec.price_input / _MILLION
        + Decimal(usage.output_tokens) * spec.price_output / _MILLION
    )
    return Cost(amount=amount, currency=spec.currency)


def estimate_max_cost(spec: ModelSpec, input_tokens: int) -> Decimal:
    """Upper bound shown in ASK_CHOICE: the summary is assumed no longer than the input."""
    output_tokens = min(input_tokens, spec.max_output_tokens)
    return (
        Decimal(input_tokens) * spec.price_input / _MILLION
        + Decimal(output_tokens) * spec.price_output / _MILLION
    )


def fits(spec: ModelSpec, input_tokens: int) -> bool:
    """I17: the prompt is under the app limit and, with the answer, inside the model context."""
    return (
        input_tokens <= MAX_INPUT_TOKENS
        and input_tokens + spec.max_output_tokens <= spec.context_window
    )
