from __future__ import annotations

import dataclasses
import io
import json
import unittest
from decimal import Decimal
from pathlib import Path

from meeting_recorder.llm import outcomes
from meeting_recorder.llm.ui import (
    Choice,
    ChoiceScreen,
    JsonLinesUI,
    ModelOption,
    ProtocolError,
    RetryScreen,
    format_max_cost,
    language_label,
    validate_choice,
)


SCREEN = ChoiceScreen(
    transcript_name="transcript.speakers.txt",
    language_label=language_label("auto"),
    word_count=420,
    options=(
        ModelOption("claude-haiku-5-5", "Claude Haiku 5.5", "max ~$0.01", True),
        ModelOption(
            "gpt-6-luna", "GPT-6 Luna", None, False, "transcript troppo lungo"
        ),
    ),
)


def ui_with(answer: str | None):
    stdin = io.StringIO("" if answer is None else answer + "\n")
    stdout = io.StringIO()
    return JsonLinesUI(stdin, stdout), stdout


class ScreenTests(unittest.TestCase):
    def test_auto_language_is_shown_as_automatic_detection(self):
        self.assertEqual(language_label("auto"), "rilevamento automatico")

    def test_screen_has_no_field_for_a_preselected_value(self):
        names = {f.name for f in dataclasses.fields(ChoiceScreen)}
        names |= {f.name for f in dataclasses.fields(ModelOption)}
        names |= {f.name for f in dataclasses.fields(RetryScreen)}
        for name in names:
            for forbidden in ("default", "select", "checked", "initial", "choice"):
                self.assertNotIn(forbidden, name)

    def test_max_cost_format(self):
        self.assertEqual(format_max_cost(Decimal("0.0051")), "max ~$0.006")
        self.assertEqual(format_max_cost(Decimal("0.09")), "max ~$0.090")
        self.assertEqual(format_max_cost(Decimal("0.0001")), "max ~$0.001")
        self.assertEqual(format_max_cost(Decimal("0")), "max ~$0.001")
        self.assertEqual(format_max_cost(Decimal("0.101")), "max ~$0.11")


class JsonLinesUITests(unittest.TestCase):
    def test_ask_choice_writes_one_event_and_reads_one_answer(self):
        ui, out = ui_with('{"model_id": "claude-haiku-5-5", "response_language": "es"}')
        choice = ui.ask_choice(SCREEN)
        self.assertEqual(choice, Choice("claude-haiku-5-5", "es"))
        (line,) = out.getvalue().splitlines()
        event = json.loads(line)
        self.assertEqual(event["event"], "ask_choice")
        screen = event["screen"]
        self.assertEqual(screen["language"], "rilevamento automatico")
        self.assertEqual(screen["word_count"], 420)
        self.assertEqual(
            [o["model_id"] for o in screen["options"]],
            ["claude-haiku-5-5", "gpt-6-luna"],
        )
        self.assertEqual(screen["options"][1]["enabled"], False)
        self.assertEqual(
            screen["options"][1]["disabled_reason"], "transcript troppo lungo"
        )
        self.assertEqual(screen["options"][0]["max_cost"], "max ~$0.01")
        self.assertEqual(
            [l["label"] for l in screen["response_languages"]],
            ["Ita", "Eng", "Esp", "Auto"],
        )
        self.assertEqual(screen["none_option"]["label"], "Nessuno")
        blob = json.dumps(event).lower()
        for forbidden in ("selected", "preselect", "default", "checked"):
            self.assertNotIn(forbidden, blob)

    def test_nessuno_needs_no_language(self):
        ui, _ = ui_with('{"model_id": null, "response_language": null}')
        self.assertEqual(ui.ask_choice(SCREEN), Choice(None, None))

    def test_invalid_answers_are_protocol_errors(self):
        bad_answers = [
            None,  # input closed
            "not json",
            "[1, 2]",
            "{}",  # no model_id
            '{"model_id": "unknown-model", "response_language": "it"}',
            '{"model_id": "gpt-6-luna", "response_language": "it"}',  # disabled
            '{"model_id": "claude-haiku-5-5"}',  # no language
            '{"model_id": "claude-haiku-5-5", "response_language": "fr"}',
            '{"model_id": 7, "response_language": "it"}',
        ]
        for answer in bad_answers:
            with self.subTest(answer=answer):
                ui, _ = ui_with(answer)
                with self.assertRaises(ProtocolError):
                    ui.ask_choice(SCREEN)

    def test_validate_choice_accepts_only_what_the_screen_offers(self):
        validate_choice(SCREEN, Choice(None, None))
        validate_choice(SCREEN, Choice("claude-haiku-5-5", "auto"))
        with self.assertRaises(ProtocolError):
            validate_choice(SCREEN, Choice("gpt-6-luna", "auto"))

    def test_ask_retry(self):
        for answer, expected in (('{"retry": true}', True), ('{"retry": false}', False)):
            ui, out = ui_with(answer)
            self.assertIs(ui.ask_retry(RetryScreen("Il servizio non ha risposto")), expected)
            event = json.loads(out.getvalue())
            self.assertEqual(event["event"], "ask_retry")
            self.assertEqual(event["message"], "Il servizio non ha risposto")
            self.assertNotIn("default", event)
        for answer in ('{}', '{"retry": "yes"}', '{"retry": null}', None):
            with self.subTest(answer=answer):
                ui, _ = ui_with(answer)
                with self.assertRaises(ProtocolError):
                    ui.ask_retry(RetryScreen("x"))

    def test_progress_and_outcome_events(self):
        ui, out = ui_with(None)
        ui.progress("Claude Haiku 5.5")
        ui.outcome(
            outcomes.done(
                run_id="r1",
                result_path=Path("/tmp/s/summaries/r1.md"),
                model_label="Claude Haiku 5.5",
                total_cost=Decimal("0.00035"),
                status="complete",
            )
        )
        progress, outcome = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(progress, {"event": "progress", "model": "Claude Haiku 5.5"})
        self.assertEqual(outcome["event"], "outcome")
        self.assertEqual(outcome["kind"], "DONE")
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(outcome["result_path"], "/tmp/s/summaries/r1.md")
        self.assertEqual(outcome["run_id"], "r1")


class OutcomeMessageTests(unittest.TestCase):
    def test_messages_follow_the_ui_table(self):
        path = Path("/tmp/s/transcripts/transcript.speakers.txt")
        self.assertIn(
            "Transcript troppo breve per un riassunto, salvato in",
            outcomes.no_llm("too_short", run_id="r", transcript_path=path).message,
        )
        self.assertEqual(
            outcomes.no_llm("no_keys", run_id="r", transcript_path=path).message,
            "Transcript salvato. Nessun LLM configurato",
        )
        self.assertIn(
            "oltre ~5 ore",
            outcomes.no_llm("too_long", run_id="r", transcript_path=path).message,
        )
        self.assertIn(
            "GPT-6 Luna",
            outcomes.no_llm(
                "too_long", run_id="r", transcript_path=path, model_label="GPT-6 Luna"
            ).message,
        )
        self.assertEqual(
            outcomes.no_llm("declined", run_id="r", transcript_path=path).message,
            "Transcript salvato",
        )
        self.assertEqual(
            outcomes.error("InsufficientCreditsError", run_id="r", provider="openai").message,
            "Credito esaurito su OpenAI. Transcript salvato",
        )
        truncated = outcomes.done(
            run_id="r",
            result_path=Path("/x.md"),
            model_label="M",
            total_cost=None,
            status="truncated",
        )
        self.assertIn("troncata per limite di lunghezza", truncated.message)
        self.assertIn("costo non disponibile", truncated.message)


if __name__ == "__main__":
    unittest.main()
