"""Startup check (offline) and housekeeping (docs/flow/flow.md, "Precondizione").

No network, no API key validation (that happens when the key is saved, I4).
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .adapters import AdapterRegistry
from .models import MAX_INPUT_TOKENS, MODEL_TABLE, ModelSpec
from .prompts import TEMPLATE_LANGUAGES, template_path


# A live writer may own a fresh .tmp file (atomic writes put it down and rename
# it a moment later), so only files older than this are orphans.
ORPHAN_TEMP_MIN_AGE_SECONDS = 10 * 60
TEMP_PATTERN = ".*.tmp"
LOG_NAMES = (".menubar.stdout.log", ".menubar.stderr.log")
LOG_MAX_BYTES = 5 * 1024 * 1024


def startup_check(
    *,
    registry: AdapterRegistry,
    models: Sequence[ModelSpec] = MODEL_TABLE,
    templates_dir: Path | None = None,
    max_input_tokens: int = MAX_INPUT_TOKENS,
) -> list[str]:
    """Offline check run when the app starts. An empty list means ok; any
    problem keeps the LLM flow from starting."""
    problems: list[str] = []

    for language in TEMPLATE_LANGUAGES:
        path = template_path(language, templates_dir)
        try:
            empty = not path.read_text(encoding="utf-8").strip()
        except OSError:
            problems.append(f"prompt template missing: {language}")
            continue
        if empty:
            problems.append(f"prompt template empty: {language}")

    for spec in models:
        name = spec.model_id or "<no model_id>"
        for field_name in ("provider", "model_id", "label"):
            if not getattr(spec, field_name):
                problems.append(f"model {name}: {field_name} missing")
        for field_name in (
            "price_input",
            "price_output",
            "max_output_tokens",
            "context_window",
        ):
            value = getattr(spec, field_name)
            if value is None or value <= 0:
                problems.append(f"model {name}: {field_name} missing")
        threshold = spec.long_context_threshold
        if threshold is not None and max_input_tokens > threshold:
            problems.append(
                f"model {name}: MAX_INPUT_TOKENS ({max_input_tokens}) exceeds "
                f"long_context_threshold ({threshold})"
            )

    for provider in dict.fromkeys(spec.provider for spec in models):
        if not registry.has(provider):
            problems.append(f"adapter not registered: {provider}")

    return problems


@dataclass
class HousekeepingResult:
    removed_temp_files: list[Path] = field(default_factory=list)
    rotated_logs: list[Path] = field(default_factory=list)


def _remove_orphan_temp_files(
    directory: Path, now: float, result: HousekeepingResult
) -> None:
    for root, _, names in os.walk(directory):
        for name in fnmatch.filter(names, TEMP_PATTERN):
            path = Path(root) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if now - path.stat().st_mtime < ORPHAN_TEMP_MIN_AGE_SECONDS:
                    continue
                path.unlink()
            except OSError:
                continue  # gone already, or not ours to remove
            result.removed_temp_files.append(path)


def _rotate_logs(directory: Path, result: HousekeepingResult) -> None:
    for name in LOG_NAMES:
        log = directory / name
        try:
            if not log.is_file() or log.stat().st_size <= LOG_MAX_BYTES:
                continue
            rotated = directory / f"{name}.1"
            # launchd keeps the log open: renaming it would leave the app
            # writing into the rotated file. Copy, then truncate in place.
            temporary = directory / f".{name}.1.rotating.tmp"
            try:
                shutil.copyfile(log, temporary)
                os.replace(temporary, rotated)
            finally:
                temporary.unlink(missing_ok=True)
            with log.open("r+b") as handle:
                handle.truncate(0)
        except OSError:
            continue
        result.rotated_logs.append(log)


def housekeeping(
    dirs: Iterable[Path], now: float | None = None
) -> HousekeepingResult:
    """Removes orphan `.*.tmp` files (recursively) and rotates the menu bar
    logs found directly in each directory. `now` is epoch seconds."""
    moment = time.time() if now is None else now
    result = HousekeepingResult()
    for directory in dirs:
        if not directory.is_dir():
            continue
        _remove_orphan_temp_files(directory, moment, result)
        _rotate_logs(directory, result)
    return result
