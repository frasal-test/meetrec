from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "meeting_recorder"
ADAPTERS = PACKAGE / "llm" / "adapters"
FORBIDDEN = ("import anthropic", "import openai", "from anthropic", "from openai")


class SdkIsolationTests(unittest.TestCase):
    def test_T34_no_module_outside_the_adapters_imports_a_provider_sdk(self):
        offenders = []
        for path in PACKAGE.rglob("*.py"):
            if ADAPTERS in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in FORBIDDEN:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO)}: {needle}")
        self.assertEqual(offenders, [])

    def test_T34_importing_the_llm_package_does_not_import_an_sdk(self):
        code = (
            "import importlib, pkgutil, sys\n"
            "import meeting_recorder.llm as pkg\n"
            "for info in pkgutil.walk_packages(pkg.__path__, 'meeting_recorder.llm.'):\n"
            "    importlib.import_module(info.name)\n"
            "loaded = sorted(m for m in sys.modules\n"
            "                if m.split('.')[0] in ('anthropic', 'openai'))\n"
            "assert not loaded, loaded\n"
            "print('ok')\n"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO))
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
