"""TRANSCRIPT_READY and SAVE_TRANSCRIPT, the worker side of the LLM flow
(docs/flow/flow.md, "Confine tra processi"). Transcription is faked: no
model, no audio decoding, no LLM, no network."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_support import FAKE_KEY  # noqa: E402

from meeting_recorder import sessions  # noqa: E402
from meeting_recorder.config import AppConfig, TranscriptionConfig  # noqa: E402
from meeting_recorder.llm.transcript import (  # noqa: E402
    count_words,
    load_saved_transcript,
)
from meeting_recorder.session_transcription import (  # noqa: E402
    SessionProcessor,
    TranscriptSaveError,
    language_setting,
)
from meeting_recorder.sessions import (  # noqa: E402
    atomic_write_text,
    enqueue_session,
    read_json,
    run_job,
)
from meeting_recorder.transcription import (  # noqa: E402
    Transcript,
    TranscriptSegment,
    write_json,
    write_markdown,
    write_rttm,
    write_speaker_txt,
    write_srt,
    write_txt,
)

SESSION_MODULE = "meeting_recorder.session_transcription"


def fake_transcript() -> Transcript:
    return Transcript(
        source="mic.caf",
        model="medium",
        language="it",
        language_probability=1.0,
        duration=3.0,
        segments=[
            TranscriptSegment(0.0, 1.5, " Buongiorno a tutti", speaker="ME"),
            TranscriptSegment(1.5, 3.0, " Buongiorno", speaker="ME"),
        ],
    )


class WorkerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session = self.root / "2026-10-17T10-00-00"
        (self.session / "audio").mkdir(parents=True)
        (self.session / "audio" / "mic.caf").write_bytes(b"audio")
        sessions.atomic_write_json(
            self.session / "meta.json",
            {"state": "recorded", "tracks": {"mic": "audio/mic.caf"}},
        )
        # The processor reports progress into job.json, so the job exists.
        enqueue_session(self.session, max_attempts=3)

        # The two things that would load a model and decode audio.
        patches = [
            mock.patch(f"{SESSION_MODULE}.load_model", return_value=object()),
            mock.patch(
                f"{SESSION_MODULE}.transcribe_source",
                return_value=fake_transcript(),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def processor(self, language: str | None = "it") -> SessionProcessor:
        config = AppConfig(
            recordings_dir=self.root,
            transcription=TranscriptionConfig(
                language=language, diarize_system=False
            ),
        )
        return SessionProcessor(config)

    def run_processor(self, processor: SessionProcessor) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            processor(self.session)

    def log_records(self) -> list[dict]:
        path = self.session / "llm.log"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def transitions(self) -> list[tuple[str, str]]:
        return [(r["from"], r["to"]) for r in self.log_records() if "from" in r]

    def leftovers(self) -> list[Path]:
        return sorted(self.session.rglob(".*.tmp"))


class NoLLMGuard:
    """T4/T45: nothing of the LLM flow may run in the worker."""

    def __enter__(self) -> "NoLLMGuard":
        self.stack = contextlib.ExitStack()
        self.run_flow = self.stack.enter_context(
            mock.patch("meeting_recorder.llm.flow.run_llm_flow")
        )
        self.create_adapter = self.stack.enter_context(
            mock.patch(
                "meeting_recorder.llm.adapters.AdapterRegistry.create"
            )
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self.stack.close()

    def assert_untouched(self, test: unittest.TestCase) -> None:
        test.assertFalse(self.run_flow.called)
        test.assertFalse(self.create_adapter.called)


class SaveTranscriptFailureTests(WorkerCase):
    def test_T04_transcript_save_failure_ends_fatal_without_llm(self) -> None:
        with NoLLMGuard() as guard, mock.patch(
            f"{SESSION_MODULE}.write_srt",
            side_effect=OSError(f"disk full, key {FAKE_KEY}"),
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(TranscriptSaveError) as caught:
                    self.processor()(self.session)
        guard.assert_untouched(self)

        error = caught.exception
        self.assertIn("OSError", str(error))
        self.assertNotIn(FAKE_KEY, str(error))
        # Nothing of the cause travels with it.
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

        records = self.log_records()
        self.assertEqual(
            self.transitions(),
            [
                ("TRANSCRIPT_READY", "SAVE_TRANSCRIPT"),
                ("SAVE_TRANSCRIPT", "END_FATAL"),
            ],
        )
        self.assertEqual(records[-1]["reason"], "OSError")
        self.assertEqual(
            # Called straight, not through run_job: the job has attempt 0.
            {(r["run_id"], r["attempt"]) for r in records}, {(None, 0)}
        )
        self.assertNotIn(
            FAKE_KEY, (self.session / "llm.log").read_text(encoding="utf-8")
        )
        # No screen: the worker prints nothing but its own progress line, and
        # no summary was started.
        self.assertNotIn('"event"', stdout.getvalue())
        self.assertFalse((self.session / "summaries").exists())
        # transcript.json is written last: without it nothing downstream
        # mistakes a partial save for a transcript.
        self.assertFalse((self.session / "transcripts" / "transcript.json").exists())

    def test_a_failed_rename_leaves_no_temporary_file(self) -> None:
        real_replace = sessions.os.replace

        def replace(source, target):
            if str(target).endswith(".srt"):
                raise OSError("rename refused")
            return real_replace(source, target)

        with mock.patch.object(sessions.os, "replace", side_effect=replace):
            with self.assertRaises(TranscriptSaveError):
                self.run_processor(self.processor())
        self.assertEqual(self.leftovers(), [])
        self.assertFalse((self.session / "transcripts" / "transcript.json").exists())

    def test_T45_save_failure_on_every_attempt_reports_fatal_only_at_the_end(
        self,
    ) -> None:
        states = []
        with NoLLMGuard() as guard, mock.patch(
            f"{SESSION_MODULE}.write_txt", side_effect=OSError("read-only")
        ):
            for _ in range(3):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run_job(self.session, self.processor())
                states.append(result.state)
                job = read_json(self.session / "job.json")
                # What the app reads: END_FATAL is shown only once the job is
                # failed, whatever last_error says in between.
                self.assertTrue(job["last_error"].startswith("TranscriptSaveError"))
        guard.assert_untouched(self)

        self.assertEqual(states, ["pending", "pending", "failed"])
        final = read_json(self.session / "job.json")
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["attempts"], 3)
        self.assertTrue(final["last_error"].startswith("TranscriptSaveError: "))

        # Every attempt restarts from TRANSCRIPT_READY (flow.md).
        sequence = [
            (r["attempt"], r["from"], r["to"])
            for r in self.log_records()
            if "from" in r
        ]
        self.assertEqual(
            sequence,
            [
                (n, a, b)
                for n in (1, 2, 3)
                for a, b in (
                    ("TRANSCRIPT_READY", "SAVE_TRANSCRIPT"),
                    ("SAVE_TRANSCRIPT", "END_FATAL"),
                )
            ],
        )

    def test_a_later_attempt_can_succeed_after_a_save_failure(self) -> None:
        with mock.patch(
            f"{SESSION_MODULE}.write_txt", side_effect=OSError("busy")
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                first = run_job(self.session, self.processor())
        with contextlib.redirect_stdout(io.StringIO()):
            second = run_job(self.session, self.processor())
        self.assertEqual((first.state, second.state), ("pending", "complete"))
        self.assertEqual(
            self.transitions()[-3:],
            [
                ("SAVE_TRANSCRIPT", "END_FATAL"),
                ("TRANSCRIPT_READY", "SAVE_TRANSCRIPT"),
                ("SAVE_TRANSCRIPT", "CHECK_LENGTH"),
            ],
        )


class SaveTranscriptOkTests(WorkerCase):
    def test_all_files_are_written_atomically(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_job(self.session, self.processor())
        self.assertEqual(result.state, "complete")
        names = sorted(p.name for p in (self.session / "transcripts").iterdir())
        self.assertEqual(
            names,
            [
                "transcript.json",
                "transcript.md",
                "transcript.speakers.txt",
                "transcript.srt",
                "transcript.txt",
            ],
        )
        self.assertEqual(self.leftovers(), [])

    def test_log_lines_for_the_successful_save(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            run_job(self.session, self.processor())
        self.assertEqual(
            self.transitions(),
            [
                ("TRANSCRIPT_READY", "SAVE_TRANSCRIPT"),
                ("SAVE_TRANSCRIPT", "CHECK_LENGTH"),
            ],
        )
        for record in self.log_records():
            self.assertIsNone(record["run_id"])
            self.assertEqual(record["attempt"], 1)
            self.assertEqual(
                set(record), {"ts", "run_id", "attempt", "from", "to", "reason"}
            )

    def test_language_setting_is_saved_and_read_back_by_the_flow(self) -> None:
        cases = [
            ("it", "it"),
            ("en", "en"),
            ("es", "es"),
            ("auto", "auto"),
            (None, "auto"),
            # No prompt template exists for it: auto, so I6 picks English.
            ("fr", "auto"),
        ]
        for configured, expected in cases:
            with self.subTest(configured=configured):
                transcripts = self.session / "transcripts"
                if transcripts.exists():
                    for path in transcripts.iterdir():
                        path.unlink()
                self.run_processor(self.processor(configured))
                saved = read_json(transcripts / "transcript.json")
                self.assertEqual(saved["language_setting"], expected)
                # The rest of the transcript is still there.
                self.assertEqual(len(saved["segments"]), 2)
                loaded = load_saved_transcript(self.session)
                self.assertEqual(loaded.language, expected)
                self.assertGreater(count_words(loaded.text), 0)

    def test_language_setting_function(self) -> None:
        self.assertEqual(language_setting(" IT "), "it")
        self.assertEqual(language_setting(""), "auto")
        self.assertEqual(language_setting("zz"), "auto")

    def test_the_worker_does_not_start_the_llm_flow(self) -> None:
        with NoLLMGuard() as guard:
            self.run_processor(self.processor())
        guard.assert_untouched(self)
        self.assertFalse((self.session / "summaries").exists())


class AtomicWriterTests(unittest.TestCase):
    def test_writers_leave_only_the_target_file(self) -> None:
        transcript = fake_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "out"
            write_txt(folder / "a.txt", transcript)
            write_srt(folder / "a.srt", transcript)
            write_json(folder / "a.json", transcript)
            write_markdown(folder / "a.md", transcript)
            write_speaker_txt(folder / "a.speakers.txt", transcript)
            write_rttm(folder / "a.rttm", "uri", [])
            self.assertEqual(
                sorted(p.name for p in folder.iterdir()),
                ["a.json", "a.md", "a.rttm", "a.speakers.txt", "a.srt", "a.txt"],
            )

    def test_write_json_merges_extra_at_the_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            write_json(path, fake_transcript(), extra={"language_setting": "es"})
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["language_setting"], "es")
            self.assertEqual(saved["model"], "medium")
            write_json(path, fake_transcript())
            self.assertNotIn(
                "language_setting", json.loads(path.read_text(encoding="utf-8"))
            )

    def test_a_failed_write_keeps_the_old_file_and_no_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.txt"
            atomic_write_text(path, "old\n")
            with mock.patch.object(
                sessions.os, "replace", side_effect=OSError("no")
            ):
                with self.assertRaises(OSError):
                    write_txt(path, fake_transcript())
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["t.txt"])

    def test_storage_keeps_the_public_name(self) -> None:
        from meeting_recorder.llm import storage

        self.assertIs(storage.atomic_write_text, atomic_write_text)


if __name__ == "__main__":
    unittest.main()
