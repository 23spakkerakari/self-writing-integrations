"""Connector cursors that survive a restart (spec 8.1 "Cursor checkpointing", ADR 0014).

A pull connector resumes from the cursor it last handed over with a record
(:attr:`~carto_edge.pipeline.model.RawRecord.commit_cursor`). The ingestor writes a cursor here
only after every event of every record up to it is durably in the disk buffer, so the store
is the at-least-once boundary of each source: after a crash the connector re-reads from here
and core dedupes by ``event_id`` (spec 8.1, 8.5).

:class:`CursorStore` is ``cursors.sqlite`` under the edge state directory: SQLite in WAL mode
with ``synchronous=NORMAL`` and one connection behind a lock, like the disk buffer, so every
:meth:`CursorStore.set` is its own committed transaction. Cursors are opaque to everything but
their connector; here they are JSON objects, written canonically (sorted keys, no ``NaN``) and
capped at :data:`MAX_CURSOR_BYTES`. A row that does not read back as a JSON object within the
cap (a corrupted file, a hand edit) is treated as no cursor: the connector starts over, which
re-sends data but never skips it. The warning names the source and the reason, never the stored
text, because a cursor may carry a file name or a key value from the source (spec 2.3
invariant 7).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, NoReturn, Self

from carto_common.logging import get_logger
from carto_edge.connectors.base import Cursor

__all__ = ["MAX_CURSOR_BYTES", "MAX_SOURCE_ID_LEN", "CursorError", "CursorStore"]

MAX_CURSOR_BYTES: Final = 64 * 1024
"""Upper bound of one cursor's JSON text (spec 2.3 invariant 8: every input is bounded)."""
MAX_SOURCE_ID_LEN: Final = 128

_SCHEMA: Final = (
    "CREATE TABLE IF NOT EXISTS cursors ("
    "source_id TEXT PRIMARY KEY, "
    "cursor TEXT NOT NULL, "
    "updated_at_ms INTEGER NOT NULL)"
)

log = get_logger(component="carto_edge.state")


class CursorError(ValueError):
    """A cursor cannot be stored: not a JSON object, or over :data:`MAX_CURSOR_BYTES`."""


def _reject_constant(name: str) -> NoReturn:
    msg = f"non-standard JSON constant {name}"
    raise ValueError(msg)


def _encode(cursor: object) -> str:
    """Canonical JSON of a cursor; typed ``object`` because callers pass connector output."""
    if not isinstance(cursor, Mapping):
        msg = "a cursor must be a mapping"
        raise CursorError(msg)
    try:
        text = json.dumps(
            dict(cursor), sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
        )
    except (TypeError, ValueError, RecursionError) as exc:
        msg = "a cursor must be JSON-serializable without NaN or infinity"
        raise CursorError(msg) from exc
    if len(text.encode("utf-8")) > MAX_CURSOR_BYTES:
        msg = f"a cursor exceeds {MAX_CURSOR_BYTES} bytes"
        raise CursorError(msg)
    return text


def _decode(text: object) -> tuple[dict[str, Any] | None, str]:
    """The stored cursor and an empty reason, or ``None`` and why it was refused."""
    if not isinstance(text, str):
        return None, "not_text"
    if len(text.encode("utf-8")) > MAX_CURSOR_BYTES:
        return None, "too_large"
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None, "not_json"
    if not isinstance(value, dict):
        return None, "not_an_object"
    return value, ""


class CursorStore:
    """``source_id`` to cursor, durable per call. ``repr`` names the file only."""

    __slots__ = ("_clock", "_conn", "_lock", "_path")

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._path = path
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_SCHEMA)
        except sqlite3.Error:
            self._conn.close()
            raise

    def __repr__(self) -> str:
        return f"CursorStore(path={str(self._path)!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _check_source_id(source_id: str) -> None:
        if not source_id or len(source_id) > MAX_SOURCE_ID_LEN:
            msg = f"a source id must have 1 to {MAX_SOURCE_ID_LEN} characters"
            raise CursorError(msg)

    def get(self, source_id: str) -> Cursor | None:
        """The committed cursor of ``source_id``; ``None`` when there is none or it is corrupt."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM cursors WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            return None
        value, reason = _decode(row[0])
        if value is None:
            log.warning("cursor.unreadable", source_id=source_id, reason=reason)
        return value

    def set(self, source_id: str, cursor: Cursor) -> None:
        """Commit ``cursor`` for ``source_id`` (one durable transaction). Raises
        :class:`CursorError` when it is not a bounded JSON object; the old cursor then stays."""
        self._check_source_id(source_id)
        text = _encode(cursor)
        updated_ms = int(self._clock().astimezone(UTC).timestamp() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursors (source_id, cursor, updated_at_ms) VALUES (?, ?, ?) "
                "ON CONFLICT (source_id) DO UPDATE SET cursor = excluded.cursor, "
                "updated_at_ms = excluded.updated_at_ms",
                (source_id, text, updated_ms),
            )

    def all(self) -> dict[str, Cursor]:
        """Every readable cursor by source id; unreadable rows are left out with a warning."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, cursor FROM cursors ORDER BY source_id"
            ).fetchall()
        out: dict[str, Cursor] = {}
        for source_id, text in rows:
            value, reason = _decode(text)
            if value is None:
                log.warning("cursor.unreadable", source_id=str(source_id), reason=reason)
                continue
            out[str(source_id)] = value
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
