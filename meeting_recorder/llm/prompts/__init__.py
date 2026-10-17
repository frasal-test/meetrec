"""Prompt templates and BUILD_PROMPT (docs/flow/flow.md, I6-I8)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from ..models import ModelSpec, estimate_tokens
from ..types import LLMRequest


TEMPLATE_LANGUAGES = ("it", "en", "es")
RESPONSE_LANGUAGES = ("it", "en", "es", "auto")
TEMPLATE_VERSION = 2
TEMPLATES_DIR = Path(__file__).resolve().parent

# I7: always an explicit instruction in the prompt.
_RESPONSE_INSTRUCTIONS = {
    "it": "Respond in Italian.",
    "en": "Respond in English.",
    "es": "Respond in Spanish.",
    "auto": "Respond in the same language as the transcript.",
}

# The template is chosen by the transcript language (I6) and spells its
# section headings in that language. The answer may be in another one (I7),
# so the headings are restated in the response language, or left to the model
# under Auto.
_SECTION_HEADINGS = {
    "it": ("Sintesi", "Decisioni", "Azioni", "Punti aperti"),
    "en": ("Summary", "Decisions", "Action items", "Open points"),
    "es": ("Síntesis", "Decisiones", "Acciones", "Puntos abiertos"),
}


def _headings_instruction(response_language: str) -> str:
    if response_language == "auto":
        return "Write the section headings in the same language as the transcript."
    headings = ", ".join(f"## {h}" for h in _SECTION_HEADINGS[response_language])
    return f"Use these section headings, in this order: {headings}."


OPEN_TAG = "<transcript>"
CLOSE_TAG = "</transcript>"

# Any literal opening or closing tag in the text, whatever its case or spacing.
_DELIMITER_RE = re.compile(r"<\s*(/?)\s*transcript\b[^>]*>", re.IGNORECASE)


def template_id(language: str) -> str:
    return f"summary-{language}-v{TEMPLATE_VERSION}"


def template_path(language: str, templates_dir: Path | None = None) -> Path:
    return (templates_dir or TEMPLATES_DIR) / f"summary_{language}.md"


def load_template(language: str, templates_dir: Path | None = None) -> str:
    if language not in TEMPLATE_LANGUAGES:
        raise ValueError(f"No prompt template for language: {language!r}")
    return template_path(language, templates_dir).read_text(encoding="utf-8").strip()


def pick_template_language(
    transcript_language: str | None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Stato: BUILD_PROMPT. I6: it/en/es -> same; auto or missing metadata -> English."""
    if transcript_language in TEMPLATE_LANGUAGES:
        return transcript_language  # type: ignore[return-value]
    if transcript_language is None and log is not None:
        log("transcript language metadata missing: using the English template")
    return "en"


def neutralize_delimiters(text: str) -> str:
    """Stops literal transcript tags in the text from closing the delimiter (I8)."""
    return _DELIMITER_RE.sub(
        lambda match: f"[{match.group(1)}transcript]", text
    )


def wrap_transcript(text: str) -> str:
    return f"{OPEN_TAG}\n{neutralize_delimiters(text)}\n{CLOSE_TAG}"


def build_system_prompt(
    template_language: str,
    response_language: str,
    templates_dir: Path | None = None,
) -> str:
    if response_language not in RESPONSE_LANGUAGES:
        raise ValueError(f"Unknown response language: {response_language!r}")
    template = load_template(template_language, templates_dir)
    # The response language stays the last line (I7).
    return (
        f"{template}\n\n{_headings_instruction(response_language)}\n"
        f"{_RESPONSE_INSTRUCTIONS[response_language]}"
    )


def estimate_prompt_tokens(
    transcript_text: str,
    transcript_language: str | None,
    templates_dir: Path | None = None,
) -> int:
    """X of adapter-contract.md: transcript tokens + template tokens.

    Uses the longest system prompt over every response language, so that
    the estimate is an upper bound whatever the user picks later.
    """
    template_language = pick_template_language(transcript_language)
    system = max(
        (
            build_system_prompt(template_language, language, templates_dir)
            for language in RESPONSE_LANGUAGES
        ),
        key=len,
    )
    return estimate_tokens(transcript_text) + estimate_tokens(
        f"{system}\n{OPEN_TAG}\n\n{CLOSE_TAG}"
    )


def build_request(
    *,
    run_id: str,
    spec: ModelSpec,
    transcript_text: str,
    transcript_language: str | None,
    response_language: str,
    templates_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> LLMRequest:
    """Stato: BUILD_PROMPT. Picks the template (I6), adds the response
    language (I7) and wraps the transcript as data (I8)."""
    template_language = pick_template_language(transcript_language, log)
    return LLMRequest(
        run_id=run_id,
        provider=spec.provider,
        model_id=spec.model_id,
        system_prompt=build_system_prompt(
            template_language, response_language, templates_dir
        ),
        transcript=wrap_transcript(transcript_text),
        template_id=template_id(template_language),
        template_language=template_language,
        response_language=response_language,
        max_output_tokens=spec.max_output_tokens,
    )
