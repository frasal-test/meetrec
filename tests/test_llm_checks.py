from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meeting_recorder.llm.adapters import AdapterRegistry  # noqa: E402
from meeting_recorder.llm.checks import (  # noqa: E402
    LOG_MAX_BYTES,
    ORPHAN_TEMP_MIN_AGE_SECONDS,
    housekeeping,
    startup_check,
)
from meeting_recorder.llm.models import (  # noqa: E402
    MAX_INPUT_TOKENS,
    MODEL_TABLE,
    find_model,
)
from meeting_recorder.llm.prompts import TEMPLATES_DIR  # noqa: E402


def full_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    for spec in MODEL_TABLE:
        registry.register(spec.provider, lambda store: None)
    return registry


class StartupCheckTests(unittest.TestCase):
    def check(self, **kwargs):
        kwargs.setdefault("registry", full_registry())
        return startup_check(**kwargs)

    def test_a_complete_setup_passes(self):
        self.assertEqual(self.check(), [])

    def test_T01_missing_es_template_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            for language in ("it", "en"):
                shutil.copy(TEMPLATES_DIR / f"summary_{language}.md", tmp)
            problems = self.check(templates_dir=Path(tmp))
        self.assertEqual(len(problems), 1)
        self.assertIn("es", problems[0])

    def test_an_empty_template_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            for language in ("it", "en", "es"):
                shutil.copy(TEMPLATES_DIR / f"summary_{language}.md", tmp)
            (Path(tmp) / "summary_en.md").write_text("  \n")
            problems = self.check(templates_dir=Path(tmp))
        self.assertEqual(len(problems), 1)
        self.assertIn("en", problems[0])

    def test_T02_incomplete_model_row_fails_the_check(self):
        base = find_model("claude-haiku-5-5")
        for field_name in (
            "price_input", "price_output", "max_output_tokens", "context_window",
        ):
            with self.subTest(missing=field_name):
                broken = dataclasses.replace(base, **{field_name: None})
                problems = self.check(models=(broken,))
                self.assertEqual(len(problems), 1)
                self.assertIn(field_name, problems[0])
                self.assertIn("claude-haiku-5-5", problems[0])

    def test_T49_threshold_below_max_input_tokens_fails_the_check(self):
        base = find_model("claude-haiku-5-5")
        low = dataclasses.replace(base, long_context_threshold=50_000)
        problems = self.check(models=(low,))
        self.assertEqual(len(problems), 1)
        self.assertIn("long_context_threshold", problems[0])
        # Equal is fine; no threshold is fine.
        equal = dataclasses.replace(
            base, long_context_threshold=MAX_INPUT_TOKENS
        )
        self.assertEqual(self.check(models=(equal,)), [])
        none = dataclasses.replace(base, long_context_threshold=None)
        self.assertEqual(self.check(models=(none,)), [])

    def test_a_provider_without_a_registered_adapter_fails_the_check(self):
        registry = AdapterRegistry()
        registry.register("anthropic", lambda store: None)
        problems = self.check(registry=registry)
        self.assertEqual(
            sorted(problems),
            ["adapter not registered: oci", "adapter not registered: openai"],
        )


class HousekeepingTests(unittest.TestCase):
    NOW = 1_000_000.0

    def touch(self, path: Path, age_seconds: float, size: int = 1) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, (self.NOW - age_seconds, self.NOW - age_seconds))
        return path

    def test_T03_orphan_temp_files_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recordings = root / "recordings"
            config = root / "config"
            old = ORPHAN_TEMP_MIN_AGE_SECONDS + 60
            orphan = self.touch(
                recordings / "s1" / "transcripts" / ".transcript.json.ab12cd.tmp", old
            )
            orphan_summary = self.touch(
                recordings / "s1" / "summaries" / ".r1.md.zz99.tmp", old
            )
            orphan_config = self.touch(config / ".config.json.tmp", old)
            keeper = self.touch(recordings / "s1" / "transcripts" / "transcript.json", old)
            visible_tmp = self.touch(recordings / "s1" / "notes.tmp", old)
            result = housekeeping([recordings, config], now=self.NOW)
            self.assertFalse(orphan.exists())
            self.assertFalse(orphan_summary.exists())
            self.assertFalse(orphan_config.exists())
            self.assertTrue(keeper.exists())
            self.assertTrue(visible_tmp.exists())
            self.assertEqual(
                sorted(result.removed_temp_files),
                sorted([orphan, orphan_summary, orphan_config]),
            )

    def test_a_fresh_temp_file_may_belong_to_a_live_writer_and_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = self.touch(root / ".out.json.q1w2e3.tmp", 5)
            borderline = self.touch(
                root / ".b.json.q1w2e3.tmp", ORPHAN_TEMP_MIN_AGE_SECONDS - 1
            )
            result = housekeeping([root], now=self.NOW)
            self.assertTrue(fresh.exists())
            self.assertTrue(borderline.exists())
            self.assertEqual(result.removed_temp_files, [])

    def test_missing_directories_are_skipped(self):
        result = housekeeping([Path("/nonexistent/meetrec/dir")], now=self.NOW)
        self.assertEqual(result.removed_temp_files, [])
        self.assertEqual(result.rotated_logs, [])

    def test_big_logs_are_rotated_and_replace_the_older_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big = self.touch(root / ".menubar.stdout.log", 0, LOG_MAX_BYTES + 1)
            small = self.touch(root / ".menubar.stderr.log", 0, 100)
            old_rotation = self.touch(root / ".menubar.stdout.log.1", 0, 7)
            result = housekeeping([root], now=self.NOW)
            self.assertEqual(result.rotated_logs, [big])
            self.assertEqual(big.stat().st_size, 0)
            self.assertEqual(
                (root / ".menubar.stdout.log.1").stat().st_size, LOG_MAX_BYTES + 1
            )
            self.assertEqual(small.stat().st_size, 100)
            self.assertFalse((root / ".menubar.stderr.log.1").exists())
            self.assertEqual(
                [p.name for p in root.iterdir() if p.name.endswith(".tmp")], []
            )
            self.assertTrue(old_rotation.exists())


if __name__ == "__main__":
    unittest.main()
