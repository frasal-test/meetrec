"""The LLM subcommands of control.py: startup-check, doctor (two phases),
llm-run, llm-key-verify and auto-summary. Adapters, SDKs and pip are fakes:
no network, no real key, no real install."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_support import FAKE_KEY, FakeAdapter, make_transcript_text, ok  # noqa: E402

from meeting_recorder import control  # noqa: E402
from meeting_recorder.config import (  # noqa: E402
    load_config,
    set_summary_value,
)
from meeting_recorder.llm.adapters import AdapterRegistry  # noqa: E402
from meeting_recorder.llm.errors import (  # noqa: E402
    AuthError,
    ProviderError,
    UnknownLLMError,
)
from meeting_recorder.llm.models import MODEL_TABLE  # noqa: E402
from meeting_recorder.llm.outcomes import OutcomeKind  # noqa: E402
from meeting_recorder.llm.prompts import TEMPLATES_DIR  # noqa: E402


class ControlCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.config = self.root / "config" / "config.json"
        self.config.parent.mkdir()
        self.write_config({})
        # Never touch the real project directory (log rotation).
        patch = mock.patch.object(control, "PROJECT_DIR", self.project)
        patch.start()
        self.addCleanup(patch.stop)

    def write_config(self, extra: dict) -> None:
        document = {"recordings_dir": str(self.recordings), **extra}
        self.config.write_text(json.dumps(document), encoding="utf-8")

    def read_config(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def run_main(self, *argv: str, stdin: str = ""):
        """Runs control.main() as the app would. Returns (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ):
            os.environ.pop("RECORDINGS_DIR", None)
            with mock.patch.object(
                sys, "argv", ["meetrec", "--config", str(self.config), *argv]
            ), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = control.main()
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def lines(text: str) -> list[dict]:
        return [json.loads(line) for line in text.splitlines()]


def fake_registry_factory(adapters: dict | None = None, scripts: dict | None = None):
    """A registry factory (summary config -> registry) of FakeAdapters that
    read the key store they are given. `adapters` collects what was built."""
    created = adapters if adapters is not None else {}

    def factory(_summary_config) -> AdapterRegistry:
        registry = AdapterRegistry()
        for spec in MODEL_TABLE:

            def build(store, provider=spec.provider):
                adapter = FakeAdapter(
                    provider, store, (scripts or {}).get(provider)
                )
                created[provider] = adapter
                return adapter

            registry.register(spec.provider, build)
        return registry

    return factory


def incomplete_registry_factory(_summary_config) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("anthropic", lambda store: None)
    return registry


class StartupCheckCommandTests(ControlCase):
    def test_ok_prints_one_json_object_and_exits_zero(self) -> None:
        out = io.StringIO()
        code = control.startup_check_command(
            self.config, out, io.StringIO(),
            registry_factory=fake_registry_factory(),
        )
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertEqual(report["ok"], True)
        self.assertEqual(report["problems"], [])
        self.assertEqual(
            report["housekeeping"], {"removed_temp_files": [], "rotated_logs": []}
        )

    def test_a_registry_missing_a_provider_fails_the_check(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = control.startup_check_command(
            self.config, out, err, registry_factory=incomplete_registry_factory
        )
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIn("adapter not registered: openai", report["problems"])
        self.assertIn("adapter not registered: oci", report["problems"])
        self.assertIn("adapter not registered: openai", err.getvalue())

    def test_a_missing_template_fails_the_check(self) -> None:
        empty = self.root / "no-templates"
        empty.mkdir()
        out = io.StringIO()
        code = control.startup_check_command(
            self.config, out, io.StringIO(),
            registry_factory=fake_registry_factory(), templates_dir=empty,
        )
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(
            sorted(report["problems"]),
            [f"prompt template missing: {lang}" for lang in ("en", "es", "it")],
        )

    def test_T44_startup_check_makes_no_network_call(self) -> None:
        def no_network(*_a, **_k):
            raise AssertionError("network used by the startup check")

        with mock.patch.object(control, "model_access_check") as hf, mock.patch.object(
            control, "_model_access_check"
        ) as hf_inner, mock.patch.object(
            socket.socket, "connect", no_network
        ), mock.patch.object(
            socket, "getaddrinfo", no_network
        ), mock.patch.object(
            control.subprocess, "run", no_network
        ):
            code, out, _ = self.run_main("startup-check")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["ok"])
        hf.assert_not_called()
        hf_inner.assert_not_called()

    def test_main_prints_nothing_but_the_json_on_stdout(self) -> None:
        code, out, err = self.run_main("startup-check")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.lines(out)), 1)
        self.assertEqual(err, "")

    def test_housekeeping_runs_on_sessions_config_and_project_logs(self) -> None:
        old = time.time() - 3600
        session_tmp = self.recordings / "2026-10-17T10-00-00" / ".job.json.abc.tmp"
        config_tmp = self.config.parent / ".config.json.abc.tmp"
        # Inside the project directory only the logs are looked after: the
        # virtual environment and the repository are not walked.
        project_tmp = self.project / ".venv" / ".stray.abc.tmp"
        session_tmp.parent.mkdir()
        project_tmp.parent.mkdir()
        for path in (session_tmp, config_tmp, project_tmp):
            path.write_text("x", encoding="utf-8")
            os.utime(path, (old, old))
        log = self.project / ".menubar.stdout.log"
        log.write_bytes(b"x" * (6 * 1024 * 1024))

        code, out, _ = self.run_main("startup-check")
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(session_tmp.exists())
        self.assertFalse(config_tmp.exists())
        self.assertTrue(project_tmp.exists())
        self.assertEqual(
            sorted(report["housekeeping"]["removed_temp_files"]),
            sorted([str(session_tmp), str(config_tmp)]),
        )
        self.assertEqual(report["housekeeping"]["rotated_logs"], [str(log)])
        self.assertEqual(log.stat().st_size, 0)


class DoctorTests(ControlCase):
    def run_doctor(self, registry_factory, **env) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(
            control, "MENUBAR_BINARY", self.root / "missing"
        ), mock.patch.object(
            control, "pyannote_version_check", return_value=(True, "version 4.0.1")
        ), mock.patch.dict(os.environ, env):
            os.environ.pop("RECORDINGS_DIR", None)
            with contextlib.redirect_stdout(out):
                code = control.doctor(self.config, registry_factory=registry_factory)
        return code, out.getvalue()

    def test_phase_one_lines_come_first_and_phase_two_still_runs(self) -> None:
        self.write_config({"transcription": {"diarize_system": True}})
        with mock.patch.object(
            control, "model_access_check", return_value=(True, "ok")
        ) as hf:
            code, text = self.run_doctor(
                fake_registry_factory(), HF_TOKEN="hf_fake_token"
            )
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("✓ Startup check: "), lines[0])
        self.assertTrue(lines[1].startswith("✓ Housekeeping: "), lines[1])
        # Phase 2: the existing checks, network ones included.
        self.assertEqual(hf.call_count, len(control.PYANNOTE_GATED_REPOS))
        self.assertIn("✓ Model access: pyannote/segmentation-3.0: ok", lines)
        self.assertTrue(any(line.startswith("✓ Python 3.10+") for line in lines))
        self.assertTrue(any("Recordings directory" in line for line in lines))
        # The exit code also depends on the machine (swiftc, binaries).
        self.assertIn(code, (0, 1))

    def test_a_failing_startup_check_counts_in_the_exit_code(self) -> None:
        code, text = self.run_doctor(incomplete_registry_factory)
        self.assertIn("✗ Startup check: adapter not registered: openai", text)
        self.assertEqual(code, 1)

    def test_a_passing_startup_check_does_not_fail_the_doctor(self) -> None:
        _, text = self.run_doctor(fake_registry_factory())
        self.assertNotIn("✗ Startup check", text)
        self.assertNotIn("✗ Housekeeping", text)


class LlmRunCase(ControlCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = self.recordings / "2026-10-17T10-00-00"
        (self.session / "transcripts").mkdir(parents=True)
        self.set_transcript(200)
        (self.session / "transcripts" / "transcript.json").write_text(
            json.dumps({"language_setting": "it"}), encoding="utf-8"
        )
        self.emergency = self.root / "emergency"
        self.adapters: dict = {}

    def set_transcript(self, words: int) -> None:
        (self.session / "transcripts" / "transcript.speakers.txt").write_text(
            make_transcript_text(words) + "\n", encoding="utf-8"
        )

    @staticmethod
    def providers_line(*providers: str) -> str:
        return json.dumps({"type": "providers", "providers": list(providers)}) + "\n"

    @staticmethod
    def key_answer(key: str | None = FAKE_KEY) -> str:
        return json.dumps({"key": key}) + "\n"

    @staticmethod
    def answer(model_id, language="it") -> str:
        return json.dumps({"model_id": model_id, "response_language": language}) + "\n"

    def run_llm(self, stdin: str, scripts: dict | None = None):
        out = io.StringIO()
        args = argparse.Namespace(config=self.config, session=self.session)
        code = control.run_llm_command(
            args,
            io.StringIO(stdin),
            out,
            registry_factory=fake_registry_factory(self.adapters, scripts),
            emergency_dir=self.emergency,
        )
        return code, self.lines(out.getvalue()), out.getvalue()

    def summaries(self) -> list[Path]:
        return sorted((self.session / "summaries").glob("*.json"))


class LlmRunTests(LlmRunCase):
    def test_T50_providers_choice_key_outcome_done(self) -> None:
        stdin = (
            self.providers_line("anthropic")
            + self.answer("claude-haiku-5-5")
            + self.key_answer()
        )
        code, events, _ = self.run_llm(stdin)
        self.assertEqual(code, 0)
        self.assertEqual(
            [e["event"] for e in events],
            ["ask_choice", "progress", "need_key", "outcome"],
        )
        # Only the chosen provider's key is asked for, after the choice.
        self.assertEqual(events[2], {"event": "need_key", "provider": "anthropic"})
        options = events[0]["screen"]["options"]
        # Only the provider that has a key is offered, with no preselection.
        self.assertEqual([o["model_id"] for o in options], ["claude-haiku-5-5"])
        self.assertEqual(events[-1]["kind"], "DONE")
        self.assertEqual(len(self.summaries()), 1)
        self.assertEqual(len(self.adapters["anthropic"].requests), 1)

    def test_T50_declined_exits_with_the_no_llm_code_and_reads_no_key(self) -> None:
        stdin = self.providers_line("anthropic") + self.answer(None, None)
        code, events, _ = self.run_llm(stdin)
        self.assertEqual(code, control.LLM_RUN_EXIT_CODES[OutcomeKind.END_NO_LLM])
        self.assertEqual(events[-1]["kind"], "END_NO_LLM")
        self.assertEqual(events[-1]["reason"], "declined")
        # "None" reads no key at all.
        self.assertNotIn("need_key", [e["event"] for e in events])
        self.assertEqual(self.adapters["anthropic"].requests, [])

    def test_no_keys_needs_no_answer(self) -> None:
        code, events, _ = self.run_llm(self.providers_line())
        self.assertEqual(code, control.LLM_RUN_EXIT_CODES[OutcomeKind.END_NO_LLM])
        self.assertEqual(events[-1]["reason"], "no_keys")

    def test_the_language_setting_of_the_transcript_reaches_the_prompt(self) -> None:
        (self.session / "transcripts" / "transcript.json").write_text(
            json.dumps({"language_setting": "es"}), encoding="utf-8"
        )
        stdin = (
            self.providers_line("anthropic")
            + self.answer("claude-haiku-5-5")
            + self.key_answer()
        )
        self.run_llm(stdin)
        request = self.adapters["anthropic"].requests[0]
        self.assertEqual(request.template_language, "es")

    def test_automatic_mode_asks_nothing(self) -> None:
        self.write_config(
            {"summary": {"automatic": True, "default_model": "claude-haiku-5-5"}}
        )
        code, events, _ = self.run_llm(
            self.providers_line("anthropic") + self.key_answer()
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [e["event"] for e in events], ["progress", "need_key", "outcome"]
        )

    def test_startup_check_failure_does_not_start_the_flow(self) -> None:
        out = io.StringIO()
        args = argparse.Namespace(config=self.config, session=self.session)
        code = control.run_llm_command(
            args,
            io.StringIO(self.providers_line("anthropic")),
            out,
            registry_factory=incomplete_registry_factory,
            emergency_dir=self.emergency,
        )
        self.assertEqual(code, control.LLM_EXIT_STARTUP_CHECK_FAILED)
        message = json.loads(out.getvalue())
        self.assertEqual(message["event"], "startup_check_failed")
        self.assertIn("adapter not registered: openai", message["problems"])
        self.assertEqual(len(self.lines(out.getvalue())), 1)
        self.assertFalse((self.session / "llm.log").exists())

    def test_malformed_providers_lines_are_protocol_errors(self) -> None:
        bad_lines = [
            "",
            "\n",
            "not json\n",
            "[1, 2]\n",
            json.dumps({"type": "other", "providers": []}) + "\n",
            json.dumps({"type": "providers"}) + "\n",
            json.dumps({"type": "providers", "providers": {"anthropic": "x"}}) + "\n",
            json.dumps({"type": "providers", "providers": ["anthropic", 5]}) + "\n",
            # The old protocol: keys must no longer arrive at start.
            json.dumps({"type": "keys", "keys": {"anthropic": "x"}}) + "\n",
        ]
        for line in bad_lines:
            with self.subTest(line=line):
                code, events, raw = self.run_llm(line)
                self.assertEqual(code, control.LLM_EXIT_PROTOCOL_ERROR)
                self.assertEqual(events[0]["event"], "protocol_error")
                self.assertFalse((self.session / "llm.log").exists())

    def test_a_protocol_error_never_echoes_the_key(self) -> None:
        line = json.dumps({"type": "keys", "keys": {"anthropic": FAKE_KEY, "x": 1}})
        code, _, raw = self.run_llm(line + "\n")
        self.assertEqual(code, control.LLM_EXIT_PROTOCOL_ERROR)
        self.assertNotIn(FAKE_KEY, raw)
        bad_key = json.dumps({"key": {"value": FAKE_KEY}}) + "\n"
        code, events, raw = self.run_llm(
            self.providers_line("anthropic") + self.answer("claude-haiku-5-5") + bad_key
        )
        self.assertEqual(code, control.LLM_EXIT_PROTOCOL_ERROR)
        self.assertEqual(events[-1]["event"], "protocol_error")
        self.assertNotIn(FAKE_KEY, raw)

    def test_T50_a_key_that_cannot_be_read_ends_as_an_auth_error(self) -> None:
        # null: the app could not read the item (for example, access denied).
        stdin = (
            self.providers_line("anthropic")
            + self.answer("claude-haiku-5-5")
            + self.key_answer(None)
        )
        code, events, _ = self.run_llm(stdin)
        self.assertEqual(code, control.LLM_RUN_EXIT_CODES[OutcomeKind.END_ERROR])
        self.assertEqual(events[-1]["reason"], "AuthError")
        self.assertIn("check it in Settings", events[-1]["message"])
        self.assertNotIn("ask_retry", [e["event"] for e in events])

    def test_providers_outside_the_model_table_are_ignored(self) -> None:
        code, events, _ = self.run_llm(self.providers_line("someone-else"))
        self.assertEqual(events[-1]["reason"], "no_keys")

    def test_invalid_answers_are_protocol_errors(self) -> None:
        answers = [
            "",  # input closed
            "nope\n",
            "[]\n",
            json.dumps({"response_language": "it"}) + "\n",  # no model_id
            self.answer("no-such-model"),
            self.answer("claude-haiku-5-5", None),  # no language
            self.answer("claude-haiku-5-5", "fr"),
        ]
        for answer in answers:
            with self.subTest(answer=answer):
                code, events, _ = self.run_llm(
                    self.providers_line("anthropic") + answer
                )
                self.assertEqual(code, control.LLM_EXIT_PROTOCOL_ERROR)
                self.assertEqual(events[-1]["event"], "protocol_error")
                self.assertEqual(self.adapters["anthropic"].requests, [])

    def test_a_missing_transcript_is_an_internal_error(self) -> None:
        (self.session / "transcripts" / "transcript.speakers.txt").unlink()
        code, events, _ = self.run_llm(self.providers_line("anthropic"))
        self.assertEqual(code, control.LLM_EXIT_INTERNAL_ERROR)
        self.assertEqual(events[-1], {"event": "error", "error": "FileNotFoundError"})

    def test_T50_retry_is_asked_once_and_reuses_the_key(self) -> None:
        stdin = (
            self.providers_line("anthropic")
            + self.answer("claude-haiku-5-5")
            + self.key_answer()
            + json.dumps({"retry": True})
            + "\n"
        )
        code, events, _ = self.run_llm(
            stdin, scripts={"anthropic": [ProviderError(), ok()]}
        )
        self.assertEqual(code, 0)
        # Retry reuses the key: it is asked for once.
        self.assertEqual(
            [e["event"] for e in events],
            ["ask_choice", "progress", "need_key", "ask_retry", "progress", "outcome"],
        )


class ExitCodeTests(LlmRunCase):
    def test_T37_the_five_outcomes_have_five_distinct_exit_codes(self) -> None:
        table = control.LLM_RUN_EXIT_CODES
        self.assertEqual(set(table), set(OutcomeKind))
        self.assertEqual(len(set(table.values())), 5)
        special = {
            control.LLM_EXIT_STARTUP_CHECK_FAILED,
            control.LLM_EXIT_PROTOCOL_ERROR,
            control.LLM_EXIT_INTERNAL_ERROR,
        }
        self.assertEqual(len(special), 3)
        self.assertEqual(special & set(table.values()), set())
        # Python's own codes (uncaught exception, argparse) stay free.
        self.assertEqual((special | set(table.values())) & {1, 2}, set())

    def test_T37_real_runs_end_with_their_own_code(self) -> None:
        keys = self.providers_line("anthropic")
        choose = keys + self.answer("claude-haiku-5-5") + self.key_answer()
        results = {}

        results["DONE"] = self.run_llm(choose)[0]
        results["END_NO_LLM"] = self.run_llm(keys + self.answer(None, None))[0]
        results["END_ERROR"] = self.run_llm(
            choose, scripts={"anthropic": [AuthError()]}
        )[0]
        # A file where summaries/ should be: SAVE_RESULT cannot succeed.
        shutil_target = self.session / "summaries"
        for child in shutil_target.glob("*"):
            child.unlink()
        shutil_target.rmdir()
        shutil_target.write_text("in the way", encoding="utf-8")
        code, events, _ = self.run_llm(choose)
        results["END_SAVE_ERROR"] = code
        self.assertEqual(events[-1]["kind"], "END_SAVE_ERROR")

        for kind, code in results.items():
            self.assertEqual(code, control.LLM_RUN_EXIT_CODES[OutcomeKind(kind)], kind)
        self.assertEqual(len(set(results.values())), 4)
        # END_FATAL is produced by the worker (SAVE_TRANSCRIPT), never by this
        # process; its code is reserved in the table.
        self.assertNotIn(control.LLM_RUN_EXIT_CODES[OutcomeKind.END_FATAL], results.values())


class KeyNeverLeaksTests(LlmRunCase):
    def test_T35_the_fake_key_appears_nowhere(self) -> None:
        keys = self.providers_line("anthropic", "openai", "oci")
        key = self.key_answer()
        runs = [
            (keys + self.answer("claude-haiku-5-5") + key, None),
            (
                keys + self.answer("claude-haiku-5-5") + key,
                {"anthropic": [RuntimeError(FAKE_KEY)]},
            ),
            (keys + self.answer("gpt-6-luna") + key, {"openai": [AuthError()]}),
            (keys + self.answer(None, None), None),
            (keys + "garbage " + FAKE_KEY + "\n", None),
            (keys + self.answer("claude-haiku-5-5") + "garbage " + FAKE_KEY + "\n", None),
        ]
        seen = []
        for stdin, scripts in runs:
            seen.append(self.run_llm(stdin, scripts)[2])
        on_disk = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and path != self.config
        )
        for text in seen + [on_disk]:
            self.assertNotIn(FAKE_KEY, text)
        self.assertTrue(self.summaries())

    def test_main_llm_run_keeps_stdout_for_the_protocol(self) -> None:
        # Real registry, real templates: with no keys the flow ends at CHECK_KEYS
        # without a call. A stray print inside the command goes to stderr.
        real = control.run_llm_command

        def noisy(args, stdin, stdout, **kwargs):
            print("stray output from a library")
            return real(args, stdin, stdout, emergency_dir=self.emergency)

        with mock.patch.object(control, "run_llm_command", noisy):
            code, out, err = self.run_main(
                "llm-run", str(self.session), stdin=self.providers_line()
            )
        self.assertEqual(code, control.LLM_RUN_EXIT_CODES[OutcomeKind.END_NO_LLM])
        events = self.lines(out)
        self.assertEqual([e["event"] for e in events], ["outcome"])
        self.assertIn("stray output", err)
        self.assertNotIn("stray", out)


class KeyVerifyTests(ControlCase):
    def verify(self, *argv: str, stdin: str = FAKE_KEY + "\n", verifier=None,
               importable=True, installer=None):
        args = control.parse_args(["llm-key-verify", *argv])
        args.config = self.config
        out, err = io.StringIO(), io.StringIO()
        installer = installer or mock.Mock(return_value=True)
        with mock.patch.object(
            control, "sdk_importable", return_value=importable
        ), mock.patch.object(control, "install_summary_requirements", installer):
            code = control.key_verify_command(
                args, io.StringIO(stdin), out, err,
                verifier_factory=verifier or (lambda provider, region: SimpleNamespace(verify_key=lambda key: None)),
            )
        return code, out.getvalue(), err.getvalue(), installer

    def test_ok(self) -> None:
        seen = []
        code, out, err, _ = self.verify(
            "--provider", "anthropic",
            verifier=lambda p, r: SimpleNamespace(verify_key=seen.append),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True})
        self.assertEqual(seen, [FAKE_KEY])
        self.assertNotIn(FAKE_KEY, out + err)

    def test_the_key_is_read_from_stdin_and_stripped(self) -> None:
        seen = []
        self.verify(
            "--provider", "openai", stdin=f"  {FAKE_KEY}  \nignored\n",
            verifier=lambda p, r: SimpleNamespace(verify_key=seen.append),
        )
        self.assertEqual(seen, [FAKE_KEY])

    def test_failure_carries_the_user_message_and_never_the_key(self) -> None:
        def verify_key(key):
            raise AuthError()

        code, out, err, _ = self.verify(
            "--provider", "anthropic",
            verifier=lambda p, r: SimpleNamespace(verify_key=verify_key),
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"ok": False, "message": AuthError().user_message})
        self.assertNotIn(FAKE_KEY, out + err)

    def test_an_unexpected_exception_does_not_show_its_text(self) -> None:
        def verify_key(key):
            raise RuntimeError(f"boom {FAKE_KEY}")

        code, out, err, _ = self.verify(
            "--provider", "openai",
            verifier=lambda p, r: SimpleNamespace(verify_key=verify_key),
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["message"], UnknownLLMError().user_message)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_an_empty_key_fails_without_installing_anything(self) -> None:
        code, out, _, installer = self.verify(
            "--provider", "anthropic", stdin="\n", importable=False
        )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        installer.assert_not_called()

    def test_missing_sdk_installs_the_requirements_once_then_retries(self) -> None:
        probes = iter([False, True])
        installer = mock.Mock(return_value=True)
        verified = []
        args = control.parse_args(["llm-key-verify", "--provider", "anthropic"])
        args.config = self.config
        out = io.StringIO()
        with mock.patch.object(
            control, "sdk_importable", side_effect=lambda module: next(probes)
        ), mock.patch.object(control, "install_summary_requirements", installer):
            code = control.key_verify_command(
                args, io.StringIO(FAKE_KEY + "\n"), out, io.StringIO(),
                verifier_factory=lambda p, r: SimpleNamespace(verify_key=verified.append),
            )
        self.assertEqual(code, 0)
        installer.assert_called_once_with()
        self.assertEqual(verified, [FAKE_KEY])
        self.assertEqual(json.loads(out.getvalue()), {"ok": True})

    def test_a_failed_install_is_reported_without_calling_the_provider(self) -> None:
        called = []
        code, out, _, installer = self.verify(
            "--provider", "openai", importable=False,
            installer=mock.Mock(return_value=False),
            verifier=lambda p, r: called.append(1),
        )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        installer.assert_called_once_with()
        self.assertEqual(called, [])

    def test_the_installer_runs_pip_in_the_running_interpreter(self) -> None:
        with mock.patch.object(
            control.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertTrue(control.install_summary_requirements())
        command = run.call_args.args[0]
        self.assertEqual(
            command[:5], [sys.executable, "-m", "pip", "install", "-r"]
        )
        self.assertTrue(command[5].endswith("requirements-summary.txt"))
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_oci_with_an_unsupported_region_fails_without_a_call(self) -> None:
        factory = mock.Mock()
        for region in (None, "", "mars-1", "EU-FRANKFURT-1"):
            with self.subTest(region=region):
                argv = ["--provider", "oci"] + (["--region", region] if region is not None else [])
                code, out, err, installer = self.verify(
                    *argv, verifier=factory, importable=False
                )
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(out)["ok"])
                self.assertIn("eu-frankfurt-1", json.loads(out)["message"])
                installer.assert_not_called()
        factory.assert_not_called()
        self.assertNotIn("oci_region", self.read_config().get("summary", {}))

    def test_oci_success_stores_the_region_and_keeps_the_rest(self) -> None:
        self.write_config({"on_stop": "/bin/true", "summary": {"automatic": False}})
        regions = []
        code, out, _, _ = self.verify(
            "--provider", "oci", "--region", "eu-frankfurt-1",
            verifier=lambda p, r: regions.append(r) or SimpleNamespace(verify_key=lambda k: None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(regions, ["eu-frankfurt-1"])
        saved = self.read_config()
        self.assertEqual(saved["summary"]["oci_region"], "eu-frankfurt-1")
        self.assertEqual(saved["on_stop"], "/bin/true")
        self.assertEqual(load_config(self.config).summary.oci_region, "eu-frankfurt-1")
        self.assertNotIn(FAKE_KEY, json.dumps(saved))

    def test_oci_failure_does_not_store_the_region(self) -> None:
        def verify_key(key):
            raise AuthError()

        code, _, _, _ = self.verify(
            "--provider", "oci", "--region", "eu-frankfurt-1",
            verifier=lambda p, r: SimpleNamespace(verify_key=verify_key),
        )
        self.assertEqual(code, 1)
        self.assertNotIn("summary", self.read_config())

    def test_main_reads_the_key_from_stdin_and_prints_only_json(self) -> None:
        verifier = SimpleNamespace(verify_key=mock.Mock())
        with mock.patch.object(
            control, "make_key_verifier", return_value=verifier
        ), mock.patch.object(control, "sdk_importable", return_value=True):
            code, out, err = self.run_main(
                "llm-key-verify", "--provider", "anthropic",
                stdin=FAKE_KEY + "\n",
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True})
        verifier.verify_key.assert_called_once_with(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_the_key_is_never_an_argument(self) -> None:
        # There is no argv option to pass it: argparse would refuse one.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            control.parse_args(["llm-key-verify", "--provider", "openai", "--key", "x"])

    def test_real_verifiers_are_built_with_the_region(self) -> None:
        oci = control.make_key_verifier("oci", "eu-frankfurt-1")
        self.assertEqual(oci.provider, "oci")
        self.assertTrue(oci.is_configured.__self__._base_url.endswith("/20231130/actions/v1"))
        self.assertEqual(control.make_key_verifier("anthropic", None).provider, "anthropic")
        self.assertEqual(control.make_key_verifier("openai", None).provider, "openai")


class AutoSummaryTests(ControlCase):
    def test_status_defaults(self) -> None:
        code, out, _ = self.run_main("auto-summary", "status")
        self.assertEqual(code, 0)
        status = json.loads(out)
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(
            status,
            {
                "automatic": False,
                "default_model": None,
                "oci_region": None,
                "models": [
                    {"provider": s.provider, "model_id": s.model_id, "label": s.label}
                    for s in MODEL_TABLE
                ],
            },
        )

    def test_on_with_a_valid_model(self) -> None:
        self.write_config({"on_stop": "/bin/true", "summary": {"oci_region": "us-ashburn-1"}})
        code, out, _ = self.run_main("auto-summary", "on", "--model", "gpt-6-luna")
        self.assertEqual(code, 0)
        reply = json.loads(out)
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["automatic"])
        self.assertEqual(reply["default_model"], "gpt-6-luna")
        saved = self.read_config()
        self.assertEqual(
            saved["summary"],
            {"oci_region": "us-ashburn-1", "automatic": True, "default_model": "gpt-6-luna"},
        )
        self.assertEqual(saved["on_stop"], "/bin/true")
        self.assertEqual(saved["recordings_dir"], str(self.recordings))
        _, status, _ = self.run_main("auto-summary", "status")
        self.assertEqual(json.loads(status)["default_model"], "gpt-6-luna")

    def test_T46_on_with_an_unknown_model_changes_nothing(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        for model in ("gpt-4", ""):
            with self.subTest(model=model):
                code, out, _ = self.run_main("auto-summary", "on", "--model", model)
                self.assertEqual(code, 1)
                reply = json.loads(out)
                self.assertFalse(reply["ok"])
                self.assertIn("message", reply)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_on_requires_a_model_argument(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main("auto-summary", "on")
        self.assertNotIn("summary", self.read_config())

    def test_off_clears_the_default_model_and_keeps_everything_else(self) -> None:
        self.write_config(
            {
                "on_stop": "/bin/true",
                "summary": {
                    "automatic": True,
                    "default_model": "claude-haiku-5-5",
                    "oci_region": "eu-frankfurt-1",
                },
            }
        )
        code, out, _ = self.run_main("auto-summary", "off")
        self.assertEqual(code, 0)
        reply = json.loads(out)
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["automatic"])
        self.assertIsNone(reply["default_model"])
        saved = self.read_config()
        self.assertEqual(
            saved["summary"],
            {"automatic": False, "default_model": None, "oci_region": "eu-frankfurt-1"},
        )
        self.assertEqual(saved["on_stop"], "/bin/true")

    def test_off_on_a_fresh_config_creates_nothing_surprising(self) -> None:
        self.run_main("auto-summary", "off")
        config = load_config(self.config)
        self.assertFalse(config.summary.automatic)
        self.assertIsNone(config.summary.default_model)

    def test_no_default_model_is_ever_chosen_by_the_code(self) -> None:
        self.run_main("auto-summary", "off")
        self.run_main("auto-summary", "status")
        self.assertIsNone(load_config(self.config).summary.default_model)


class ConfigWriterTests(ControlCase):
    def test_set_summary_value_is_atomic_and_keeps_the_file(self) -> None:
        self.write_config({"on_stop": "x", "summary": {"automatic": True}})
        set_summary_value(self.config, "oci_region", "us-chicago-1")
        saved = self.read_config()
        self.assertEqual(saved["summary"], {"automatic": True, "oci_region": "us-chicago-1"})
        self.assertEqual(saved["on_stop"], "x")
        self.assertEqual([p.name for p in self.config.parent.iterdir()], ["config.json"])

    def test_it_creates_the_block_and_the_file_when_missing(self) -> None:
        path = self.root / "new" / "dir" / "config.json"
        set_summary_value(path, "automatic", False)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"summary": {"automatic": False}})

    def test_unknown_keys_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            set_summary_value(self.config, "api_key", FAKE_KEY)
        self.assertNotIn("summary", self.read_config())

    def test_a_failed_write_keeps_the_old_file(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                set_summary_value(self.config, "automatic", True)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.config.parent.iterdir()], ["config.json"])


if __name__ == "__main__":
    unittest.main()
