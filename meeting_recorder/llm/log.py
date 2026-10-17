"""Structured log of the flow transitions (I15). Never receives a key."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..sessions import utc_now


LOG_FILE = "llm.log"

Sink = Callable[[dict[str, Any]], None]


def file_sink(session_dir: Path) -> Sink:
    """One JSON line per record, appended to <session>/llm.log."""

    def write(record: dict[str, Any]) -> None:
        with (session_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return write


class FlowLog:
    def __init__(
        self,
        run_id: str,
        sink: Sink,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.run_id = run_id
        self._sink = sink
        self._clock = clock or utc_now

    def transition(self, from_state: str, to_state: str, reason: str) -> None:
        """One line per transition: ts, run_id, from, to, reason."""
        self._emit(
            {
                "ts": self._clock(),
                "run_id": self.run_id,
                "from": from_state,
                "to": to_state,
                "reason": reason,
            }
        )

    def note(self, message: str) -> None:
        """A line that is not a transition (it has no from/to)."""
        self._emit(
            {
                "ts": self._clock(),
                "run_id": self.run_id,
                "note": message,
            }
        )

    def attempt(self, record: dict[str, Any]) -> None:
        """Tokens and cost of one provider call, also when it failed (I11)."""
        self._emit(
            {"ts": self._clock(), "run_id": self.run_id, "attempt": record}
        )

    def _emit(self, record: dict[str, Any]) -> None:
        # A log that cannot be written must not take the summary down with it.
        try:
            self._sink(record)
        except OSError:
            pass
