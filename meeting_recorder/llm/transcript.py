"""The saved transcript the flow works on (docs/flow/flow.md, "Confine tra processi")."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


SPEAKERS_FILE = "transcripts/transcript.speakers.txt"
METADATA_FILE = "transcripts/transcript.json"
LANGUAGE_KEY = "language_setting"
LANGUAGE_VALUES = ("it", "en", "es", "auto")
WARNING_PREFIX = "[MeetRec]"

# "[00:00:01.000 - 00:00:03.500] ME: " in front of every spoken line.
_PREFIX_RE = re.compile(
    r"^\s*\[\d+:\d{2}:\d{2}[.,]\d{3}\s*-\s*\d+:\d{2}:\d{2}[.,]\d{3}\]"
    r"\s*[^:\n]*:\s*"
)


@dataclass(frozen=True)
class TranscriptInput:
    transcript_id: str
    path: Path
    text: str
    # it | en | es | auto, or None when the metadata is missing (I6).
    language: str | None


def read_language_setting(session_dir: Path) -> str | None:
    """The language preset stored with the transcript. Absent or invalid -> None."""
    try:
        loaded = json.loads(
            (session_dir / METADATA_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    value = loaded.get(LANGUAGE_KEY)
    return value if value in LANGUAGE_VALUES else None


def load_saved_transcript(session_dir: Path) -> TranscriptInput:
    """Reads the transcript already saved by SAVE_TRANSCRIPT, without the
    [MeetRec] warning lines."""
    path = session_dir / SPEAKERS_FILE
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(WARNING_PREFIX)
    ]
    return TranscriptInput(
        transcript_id=session_dir.name,
        path=path,
        text="\n".join(lines).strip(),
        language=read_language_setting(session_dir),
    )


def count_words(text: str) -> int:
    """Words of speech only: the timestamp and speaker prefix is not counted."""
    total = 0
    for line in text.splitlines():
        if line.startswith(WARNING_PREFIX):
            continue
        total += len(_PREFIX_RE.sub("", line, count=1).split())
    return total
