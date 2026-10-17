"""Atomic persistence of results (I2, I13, I14)."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# atomic_write_text lives in sessions (shared with the transcript writers) and
# is re-exported here so storage.atomic_write_text keeps working.
from ..sessions import atomic_write_json, atomic_write_text  # noqa: F401
from .outcomes import format_real_cost


SUMMARIES_DIR = "summaries"


def default_emergency_dir() -> Path:
    return Path("~/Library/Application Support/MeetRec/emergency").expanduser()


def new_run_id() -> str:
    """Timestamp plus a random suffix: unique per run, sortable."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def _cost_line(record: dict[str, Any], labels: dict[str, str]) -> str:
    """Total over every attempt (I11). Unknown stays unknown, never $0."""
    total = record.get("total_cost")
    attempts = len(record.get("attempts") or [])
    noun = labels["attempt"] if attempts == 1 else labels["attempts"]
    if total is None:
        return f"{labels['no_cost']} ({attempts} {noun})"
    return f"{format_real_cost(Decimal(total))} ({attempts} {noun})"


# Header of the .md, in the language of the summary below it.
_HEADER_LABELS = {
    "it": {
        "title": "Riassunto della riunione", "session": "Sessione",
        "service": "Servizio", "model": "Modello", "date": "Data",
        "cost": "Costo", "attempt": "tentativo", "attempts": "tentativi",
        "no_cost": "costo non disponibile",
        "truncated": "Attenzione: la risposta è stata troncata per limite di lunghezza",
    },
    "en": {
        "title": "Meeting summary", "session": "Session",
        "service": "Service", "model": "Model", "date": "Date",
        "cost": "Cost", "attempt": "attempt", "attempts": "attempts",
        "no_cost": "cost not available",
        "truncated": "Warning: the response was cut off at the length limit",
    },
    "es": {
        "title": "Resumen de la reunión", "session": "Sesión",
        "service": "Servicio", "model": "Modelo", "date": "Fecha",
        "cost": "Coste", "attempt": "intento", "attempts": "intentos",
        "no_cost": "coste no disponible",
        "truncated": "Atención: la respuesta se cortó por el límite de longitud",
    },
}


def _header_language(record: dict[str, Any]) -> str:
    """The response language; under Auto the summary follows the transcript,
    whose language is the template's (I6)."""
    language = record.get("response_language")
    if language not in _HEADER_LABELS:
        language = record.get("template_language")
    return language if language in _HEADER_LABELS else "en"


def render_markdown(record: dict[str, Any], model_label: str) -> str:
    """The readable file saved next to the JSON."""
    labels = _HEADER_LABELS[_header_language(record)]
    lines = [
        f"# {labels['title']}",
        "",
        f"- {labels['session']}: {record['transcript_id']}",
        f"- {labels['service']}: {record['provider']}",
        f"- {labels['model']}: {model_label}",
        f"- {labels['date']}: {record['created_at']}",
        f"- {labels['cost']}: {_cost_line(record, labels)}",
    ]
    if record["status"] == "truncated":
        lines.append(f"- {labels['truncated']}")
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
