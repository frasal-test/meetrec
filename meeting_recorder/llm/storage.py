"""Atomic persistence of results (I2, I13, I14)."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# atomic_write_text lives in sessions (shared with the transcript writers) and
# is re-exported here so storage.atomic_write_text keeps working.
from ..sessions import atomic_write_json, atomic_write_text  # noqa: F401


SUMMARIES_DIR = "summaries"


def default_emergency_dir() -> Path:
    return Path("~/Library/Application Support/MeetRec/emergency").expanduser()


def new_run_id() -> str:
    """Timestamp plus a random suffix: unique per run, sortable."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def render_markdown(record: dict[str, Any], model_label: str) -> str:
    """The readable file saved next to the JSON."""
    lines = [
        "# Riassunto della riunione",
        "",
        f"- Sessione: {record['transcript_id']}",
        f"- Servizio: {record['provider']}",
        f"- Modello: {model_label}",
        f"- Data: {record['created_at']}",
    ]
    if record["status"] == "truncated":
        lines.append(
            "- Attenzione: la risposta è stata troncata per limite di lunghezza"
        )
    return "\n".join(lines) + "\n\n" + str(record["text"]).strip() + "\n"


def save_result(
    session_dir: Path, run_id: str, record: dict[str, Any], markdown: str
) -> Path:
    """Writes summaries/<run_id>.json and .md. Never overwrites an earlier run
    (I2). Returns the .md path, the one meant to be read."""
    folder = session_dir / SUMMARIES_DIR
    json_path = folder / f"{run_id}.json"
    md_path = folder / f"{run_id}.md"
    for path in (json_path, md_path):
        if path.exists():
            raise FileExistsError(f"Result already exists: {path.name}")
    atomic_write_text(md_path, markdown)
    try:
        atomic_write_json(json_path, record)
    except Exception:
        md_path.unlink(missing_ok=True)
        raise
    return md_path


def emergency_save(record: dict[str, Any], emergency_dir: Path) -> Path:
    """EMERGENCY_SAVE: the answer must not be lost (I13)."""
    run_id = str(record["run_id"])
    path = emergency_dir / f"{run_id}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = emergency_dir / f"{run_id}-{counter}.json"
    atomic_write_json(path, record)
    return path
