from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meeting_recorder.llm import storage
from meeting_recorder.llm.log import FlowLog, file_sink
from meeting_recorder.llm.transcript import (
    count_words,
    load_saved_transcript,
)


class RunIdTests(unittest.TestCase):
    def test_run_ids_are_unique(self):
        ids = {storage.new_run_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)


class AtomicWriteTests(unittest.TestCase):
    def test_writes_text_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "out.md"
            storage.atomic_write_text(target, "ciao\n")
            self.assertEqual(target.read_text("utf-8"), "ciao\n")
            self.assertEqual([p.name for p in target.parent.iterdir()], ["out.md"])

    def test_temporary_file_is_removed_when_the_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.md"
            with mock.patch("os.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    storage.atomic_write_text(target, "x")
            self.assertEqual(list(Path(tmp).iterdir()), [])


class SaveResultTests(unittest.TestCase):
    def test_T05_two_runs_make_two_results_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            first, second = storage.new_run_id(), storage.new_run_id()
            a = storage.save_result(session, first, {"run_id": first}, "uno")
            b = storage.save_result(session, second, {"run_id": second}, "due")
            self.assertNotEqual(a, b)
            self.assertEqual(a.read_text("utf-8"), "uno")
            self.assertEqual(b.read_text("utf-8"), "due")
            self.assertEqual(
                sorted(p.name for p in (session / "summaries").glob("*.json")),
                sorted([f"{first}.json", f"{second}.json"]),
            )
            with self.assertRaises(FileExistsError):
                storage.save_result(session, first, {"run_id": first}, "again")
            self.assertEqual(a.read_text("utf-8"), "uno")

    def test_a_json_failure_does_not_leave_a_lone_markdown_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            with mock.patch.object(
                storage, "atomic_write_json", side_effect=OSError("disk")
            ):
                with self.assertRaises(OSError):
                    storage.save_result(session, "r1", {"run_id": "r1"}, "md")
            self.assertEqual(list((session / "summaries").iterdir()), [])


class MarkdownTests(unittest.TestCase):
    def record(self, **changes):
        record = {
            "transcript_id": "2026-10-17_10-00-00",
            "provider": "anthropic",
            "created_at": "2026-10-17T10:05:00+00:00",
            "status": "complete",
            "text": "## Sintesi\nTesto.",
            "attempts": [{"cost": "0.00305"}],
            "total_cost": "0.00305",
            "template_language": "it",
            "response_language": "it",
        }
        record.update(changes)
        return record

    def test_the_summary_shows_the_total_cost(self):
        markdown = storage.render_markdown(self.record(), "Claude Haiku 5.5")
        self.assertIn("- Costo: $0.0031 (1 tentativo)", markdown)

    def test_the_service_is_named_not_its_internal_id(self):
        markdown = storage.render_markdown(self.record(), "Claude Haiku 5.5")
        self.assertIn("- Servizio: Anthropic\n", markdown)
        markdown = storage.render_markdown(self.record(provider="oci"), "gpt-oss-120b (OCI)")
        self.assertIn("- Servizio: Oracle OCI\n", markdown)

    def test_two_attempts_and_an_unknown_cost(self):
        markdown = storage.render_markdown(
            self.record(attempts=[{"cost": None}, {"cost": None}], total_cost=None),
            "Claude Haiku 5.5",
        )
        self.assertIn("- Costo: costo non disponibile (2 tentativi)", markdown)

    def test_the_header_follows_the_response_language(self):
        markdown = storage.render_markdown(
            self.record(response_language="en"), "Claude Haiku 5.5"
        )
        self.assertTrue(markdown.startswith("# Meeting summary\n"))
        self.assertIn("- Session: 2026-10-17_10-00-00", markdown)
        self.assertIn("- Cost: $0.0031 (1 attempt)", markdown)
        self.assertNotIn("Riassunto", markdown)

    def test_under_auto_the_header_follows_the_transcript(self):
        markdown = storage.render_markdown(
            self.record(response_language="auto", template_language="es"),
            "Claude Haiku 5.5",
        )
        self.assertTrue(markdown.startswith("# Resumen de la reunión\n"))
        self.assertIn("- Coste: $0.0031 (1 intento)", markdown)


class EmergencyTests(unittest.TestCase):
    def test_writes_run_id_json_in_the_given_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {"run_id": "r9", "text": "salvami"}
            path = storage.emergency_save(record, Path(tmp) / "emergency")
            self.assertEqual(path, Path(tmp) / "emergency" / "r9.json")
            self.assertEqual(json.loads(path.read_text("utf-8")), record)
            again = storage.emergency_save(record, Path(tmp) / "emergency")
            self.assertNotEqual(again, path)
            self.assertTrue(path.is_file())

    def test_default_folder_is_outside_the_recordings(self):
        self.assertEqual(
            storage.default_emergency_dir().parts[-3:],
            ("Application Support", "MeetRec", "emergency"),
        )


class LogTests(unittest.TestCase):
    def test_file_sink_appends_one_json_line_per_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            log = FlowLog("r1", file_sink(session), clock=lambda: "T")
            log.transition("A", "B", "why")
            log.note("hello")
            lines = (session / "llm.log").read_text("utf-8").splitlines()
            self.assertEqual(
                [json.loads(l) for l in lines],
                [
                    {"ts": "T", "run_id": "r1", "from": "A", "to": "B", "reason": "why"},
                    {"ts": "T", "run_id": "r1", "note": "hello"},
                ],
            )

    def test_an_unwritable_log_does_not_stop_the_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = FlowLog("r1", file_sink(Path(tmp) / "missing"))
            log.transition("A", "B", "why")  # must not raise


class TranscriptLoadingTests(unittest.TestCase):
    def make_session(self, tmp: str, language_setting=None) -> Path:
        session = Path(tmp) / "2026-10-17_10-00-00"
        (session / "transcripts").mkdir(parents=True)
        (session / "transcripts" / "transcript.speakers.txt").write_text(
            "[MeetRec] Warning: microphone was quiet\n\n"
            "[00:00:00.000 - 00:00:02.000] ME: buongiorno a tutti\n"
            "[00:00:02.500 - 00:00:04.000] REMOTE_01: salve\n",
            encoding="utf-8",
        )
        if language_setting is not None:
            (session / "transcripts" / "transcript.json").write_text(
                json.dumps({"language_setting": language_setting, "segments": []}),
                encoding="utf-8",
            )
        return session

    def test_drops_warning_lines_and_reads_the_language_setting(self):
        for setting in ("it", "en", "es", "auto"):
            with tempfile.TemporaryDirectory() as tmp:
                loaded = load_saved_transcript(self.make_session(tmp, setting))
                self.assertEqual(loaded.language, setting)
                self.assertNotIn("[MeetRec]", loaded.text)
                self.assertIn("buongiorno", loaded.text)
                self.assertEqual(loaded.transcript_id, "2026-10-17_10-00-00")
                self.assertEqual(loaded.path.name, "transcript.speakers.txt")

    def test_missing_or_invalid_language_setting_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_saved_transcript(self.make_session(tmp)).language)
        with tempfile.TemporaryDirectory() as tmp:
            session = self.make_session(tmp, "klingon")
            self.assertIsNone(load_saved_transcript(session).language)
        with tempfile.TemporaryDirectory() as tmp:
            session = self.make_session(tmp)
            (session / "transcripts" / "transcript.json").write_text("{broken")
            self.assertIsNone(load_saved_transcript(session).language)

    def test_count_words_ignores_timestamp_and_speaker_prefix(self):
        text = (
            "[00:00:00.000 - 00:00:02.000] ME: uno due tre\n"
            "[00:01:00.000 - 00:01:02.000] REMOTE_01: quattro cinque\n"
            "[MeetRec] avviso che non conta\n"
        )
        self.assertEqual(count_words(text), 5)
        self.assertEqual(count_words("riga senza prefisso qui"), 4)
        self.assertEqual(count_words(""), 0)


if __name__ == "__main__":
    unittest.main()
