from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meeting_recorder.config import SummaryConfig, load_config


EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def load(raw: dict | None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(raw or {}), encoding="utf-8")
        return load_config(path)


class SummaryConfigTests(unittest.TestCase):
    def test_defaults_are_off_with_no_model(self):
        self.assertEqual(SummaryConfig(), SummaryConfig(False, None, None))
        self.assertEqual(load({}).summary, SummaryConfig())

    def test_reads_the_summary_block(self):
        config = load(
            {
                "summary": {
                    "automatic": True,
                    "default_model": "claude-haiku-5-5",
                    "oci_region": "eu-frankfurt-1",
                }
            }
        )
        self.assertEqual(
            config.summary,
            SummaryConfig(True, "claude-haiku-5-5", "eu-frankfurt-1"),
        )

    def test_empty_strings_become_none(self):
        config = load({"summary": {"default_model": "", "oci_region": ""}})
        self.assertIsNone(config.summary.default_model)
        self.assertIsNone(config.summary.oci_region)

    def test_summary_must_be_an_object(self):
        for value in ("yes", 3, None, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    load({"summary": value})

    def test_example_config_has_the_summary_block_switched_off(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["summary"],
            {"automatic": False, "default_model": None, "oci_region": None},
        )
        self.assertEqual(load(raw).summary, SummaryConfig())


if __name__ == "__main__":
    unittest.main()
