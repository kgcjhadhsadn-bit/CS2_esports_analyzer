"""Write-ahead journal of clicks on the final "Postaw zakład" button (crash-safe idempotency).

A bet must never be placed twice. Inside one process the executor remembers the idempotency keys whose
final button it clicked; this journal extends that memory across restarts and crashes:

* immediately BEFORE the final click a ``click`` record is appended and fsync-ed (write-ahead). If the
  record cannot be written the button is not clicked;
* when the outcome is known a ``result`` record follows.

A key is *blocked* (the executor answers ``UNCERTAIN`` without touching the page) while its latest record
is a ``click`` with no result (the process died around the click - the bet may exist) or a result of
``placed`` / ``uncertain``. Definite rejections (insufficient balance, odds moved, kill switch ...)
unblock the key, so those bets may be retried.

The file lives next to the profile lock in the browser-profile directory (one Betclic account = one
profile = one journal); the profile lock guarantees a single writer. It is JSON lines::

    {"event": "click", "key": "pred-17:match_winner#team1", "bet_id": "42", "at": "2026-11-23T18:30:12+00:00"}
    {"event": "result", "key": "pred-17:match_winner#team1", "bet_id": "42", "status": "placed", "at": "..."}

It never contains credentials, stakes or account data. Records older than ``retention_days`` are ignored
and dropped by :meth:`PlacementJournal.compact` (pre-match bets cannot be re-sent weeks later: the match
is over); only the process holding the profile lock may compact. A malformed line is skipped: a ``click``
record is complete (fsync) before the click happens, so a torn line can only belong to an attempt that
never clicked.

Every record is one line of its own, whatever came before and whatever the key contains: an append first
terminates a torn last line, the JSON is ASCII-only (``\\uXXXX`` escapes) and the reader splits on
``\\n`` alone. A ``click`` record that could not be read back would silently unblock its key.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cs2bot.domain.betting import PlacementStatus
from cs2bot.executor.base import ExecutorError
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import utcnow

log = get_logger(__name__)

JOURNAL_FILENAME = "cs2bot-placements.jsonl"
EVENT_CLICK = "click"
EVENT_RESULT = "result"
#: Results after which a bet may exist at the bookmaker: never click again for that key.
BLOCKING_STATUSES: frozenset[str] = frozenset({PlacementStatus.PLACED.value, PlacementStatus.UNCERTAIN.value})


class JournalError(ExecutorError):
    """The placement journal could not be read or written (real bets are refused: fail safe)."""


@dataclass(frozen=True)
class JournalEntry:
    event: str  # "click" | "result"
    key: str
    bet_id: str
    at: datetime
    status: str | None = None  # PlacementStatus value of a "result" record

    @property
    def blocks(self) -> bool:
        """Whether this (latest) record of a key means "the bet may exist at the bookmaker"."""
        return self.event == EVENT_CLICK or self.status in BLOCKING_STATUSES

    def to_json(self) -> str:
        data = {"event": self.event, "key": self.key, "bet_id": self.bet_id, "at": self.at.isoformat()}
        if self.status is not None:
            data["status"] = self.status
        # ASCII only: a raw U+2028 / U+0085 (or any other Unicode line break) inside a key would split
        # the record in two for a line-based reader, and a "click" record that cannot be read back
        # blocks nothing.
        return json.dumps(data, ensure_ascii=True)

    @classmethod
    def from_json(cls, line: str) -> JournalEntry | None:
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                return None
            event = str(data["event"])
            at = datetime.fromisoformat(str(data["at"]))
            if event not in (EVENT_CLICK, EVENT_RESULT) or at.tzinfo is None:
                return None
            status = data.get("status")
            return cls(
                event=event,
                key=str(data["key"]),
                bet_id=str(data.get("bet_id", "")),
                at=at,
                status=str(status) if status is not None else None,
            )
        except (ValueError, KeyError, TypeError):
            return None


def _lines(text: str) -> list[str]:
    """Records are separated by ``\\n`` only (``str.splitlines`` would also split on U+2028, U+0085 ...
    inside a record written by an older version with raw non-ASCII keys)."""
    return [line.rstrip("\r") for line in text.split("\n")]


class PlacementJournal:
    """Append-only record of final-button clicks and their outcomes for one browser profile."""

    def __init__(
        self,
        path: Path,
        *,
        retention_days: float = 60.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self.path = Path(path)
        self.retention = timedelta(days=retention_days)
        self._clock = clock

    # ------------------------------------------------------------------ reading

    def entries(self) -> list[JournalEntry]:
        """All well-formed records within the retention window, oldest first.

        A missing file is an empty journal. Any other read problem raises :class:`JournalError`:
        without the journal the executor cannot know what was already clicked.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalError(f"placement journal {self.path} is unreadable ({type(exc).__name__})") from exc
        cutoff = self._clock() - self.retention
        out: list[JournalEntry] = []
        skipped = 0
        for line in _lines(text):
            if not line.strip():
                continue
            entry = JournalEntry.from_json(line)
            if entry is None:
                skipped += 1
                continue
            if entry.at >= cutoff:
                out.append(entry)
        if skipped:
            log.warning("placement journal %s: skipped %d malformed line(s)", self.path, skipped)
        return out

    def latest(self, key: str) -> JournalEntry | None:
        """The most recent record of ``key`` (``None`` if it was never clicked)."""
        found: JournalEntry | None = None
        for entry in self.entries():
            if entry.key == key:
                found = entry
        return found

    def blocking_entry(self, key: str) -> JournalEntry | None:
        """The record that forbids clicking for ``key`` again, if any (re-reads the file each call)."""
        entry = self.latest(key)
        return entry if entry is not None and entry.blocks else None

    # ------------------------------------------------------------------ writing

    def record_click(self, key: str, bet_id: int | str) -> None:
        """Durably note that the final button is about to be clicked. Raises :class:`JournalError`."""
        self._append(JournalEntry(event=EVENT_CLICK, key=key, bet_id=str(bet_id), at=self._clock()))

    def record_result(self, key: str, bet_id: int | str, status: PlacementStatus) -> None:
        """Note the outcome of a journaled click. Raises :class:`JournalError` (the key stays blocked)."""
        self._append(
            JournalEntry(
                event=EVENT_RESULT, key=key, bet_id=str(bet_id), at=self._clock(), status=status.value
            )
        )

    def _append(self, entry: JournalEntry) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A torn last line (the process died inside an earlier write) has no newline: without one
            # of our own the new record would be glued to it and skipped as malformed - a "click"
            # record that does not block is exactly what this journal must never produce.
            prefix = b"" if self._ends_with_newline() else b"\n"
            with open(self.path, "ab") as fh:
                fh.write(prefix + entry.to_json().encode("utf-8") + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
        except (OSError, ValueError) as exc:  # ValueError: a key that cannot be encoded as UTF-8
            raise JournalError(
                f"could not write the placement journal {self.path} ({type(exc).__name__})"
            ) from exc

    def _ends_with_newline(self) -> bool:
        """Whether the journal file is empty / missing or ends with a line break (raises ``OSError``)."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    return True
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) == b"\n"
        except FileNotFoundError:
            return True

    def compact(self) -> int:
        """Drop expired / malformed lines (atomic rewrite); returns the number of lines removed.

        Best effort: problems are logged, never raised (the journal keeps working uncompacted).
        """
        try:
            raw_lines = [ln for ln in _lines(self.path.read_text(encoding="utf-8")) if ln.strip()]
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("could not compact the placement journal %s: %s", self.path, type(exc).__name__)
            return 0
        try:
            keep = self.entries()
        except JournalError:
            return 0
        removed = len(raw_lines) - len(keep)
        if removed <= 0:
            return 0
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.writelines(entry.to_json() + "\n" for entry in keep)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not compact the placement journal %s: %s", self.path, type(exc).__name__)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return 0
        return removed
