"""The edge audit trail: an append-only, hash-chained NDJSON file (spec 14.9, ADR 0014).

Core's audit log is a PostgreSQL table (M6). The edge has no database and writes its side of
every reveal, tokenize, key operation and configuration change to ``<state_dir>/audit.ndjson``
with the same chain rule as spec 14.9: every row carries ``prev_hash`` and
``row_hash = SHA-256(prev_hash || canonical_json(row without row_hash))``. The first row chains
to :data:`GENESIS_HASH`. ``carto-ctl audit verify`` recomputes the chain with :func:`verify`.

A row is ``seq``, ``ts``, ``action``, ``actor``, ``target``, ``request_id``, ``details``,
``prev_hash``, ``row_hash``. ``details`` is operator data about the call (counts, reasons,
purposes), never values: every string in it passes through
:func:`carto_common.logging.mask_string` before it is written (spec 2.3 invariant 7), and sizes
are bounded. Opening the file verifies the existing chain; a broken chain refuses to open so
tampering never goes unnoticed. Writes are serialized with a lock and fsynced.

Two processes append to the same file: the gateway (reveal, tokenize) and the operator CLI
(``carto-edge key rotate``, ``secret set``) run inside the same container. Every append therefore
takes an exclusive operating-system lock on ``audit.ndjson.lock`` and, when the file grew since
this writer's last append, re-reads the last row's ``seq`` and ``row_hash`` before chaining to
it, so the chain stays one chain whoever writes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, TextIO

from pydantic import AwareDatetime, BaseModel, ConfigDict

from carto_common.logging import mask_string

__all__ = [
    "GENESIS_HASH",
    "AuditChainError",
    "AuditRow",
    "EdgeAudit",
    "VerifyResult",
    "canonical_json",
    "row_hash",
    "verify",
]

GENESIS_HASH: Final = "0" * 64
MAX_ACTION_LEN: Final = 128
MAX_ACTOR_LEN: Final = 256
MAX_TARGET_LEN: Final = 1024
MAX_REQUEST_ID_LEN: Final = 128
MAX_DETAIL_KEY_LEN: Final = 128
MAX_DETAIL_VALUE_LEN: Final = 4096
MAX_DETAIL_ITEMS: Final = 64
MAX_DETAIL_DEPTH: Final = 4
HASH_KEY: Final = "row_hash"


class AuditChainError(Exception):
    """The audit file does not verify; the message names the line, never its content."""


LOCK_SUFFIX: Final = ".lock"
_TAIL_CHUNK: Final = 8192


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """An exclusive lock between processes on ``path`` (created when missing)."""
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415 - platform specific

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after about ten seconds; keep waiting
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415 - platform specific

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _tail(path: Path) -> tuple[int, str] | None:
    """``seq`` and ``row_hash`` of the last row, read from the end of the file."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        line = b""
        while position > 0:
            step = min(_TAIL_CHUNK, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
            stripped = data.rstrip(b"\n")
            cut = stripped.rfind(b"\n")
            if cut >= 0:
                line = stripped[cut + 1 :]
                break
            line = stripped
    if not line:
        return None
    try:
        row = json.loads(line)
        return int(row["seq"]), str(row[HASH_KEY])
    except (ValueError, KeyError, TypeError) as exc:
        msg = f"the last row of {path} is not an audit row"
        raise AuditChainError(msg) from exc


class AuditRow(BaseModel):
    """One written row, as returned by :meth:`EdgeAudit.record`."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    seq: int
    ts: AwareDatetime
    action: str
    actor: str
    target: str
    request_id: str
    details: dict[str, Any]
    prev_hash: str
    row_hash: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    rows: int
    first_bad_line: int | None


def canonical_json(data: object) -> str:
    """Sorted keys, no whitespace, ASCII only: the same bytes for the same row everywhere."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def row_hash(prev_hash: str, row: Mapping[str, object]) -> str:
    """``sha256(prev_hash || canonical_json(row))`` as hex; ``row`` excludes ``row_hash``."""
    return hashlib.sha256((prev_hash + canonical_json(row)).encode("utf-8")).hexdigest()


def _bound(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _clean_value(value: object, depth: int) -> object:
    """Mask strings, keep JSON scalars, recurse into containers, stringify the rest."""
    if isinstance(value, bool | int) or value is None:
        return value
    if isinstance(value, float):
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, str):
        return mask_string(_bound(value, MAX_DETAIL_VALUE_LEN))
    if isinstance(value, datetime | date):
        return mask_string(value.isoformat())
    if depth >= MAX_DETAIL_DEPTH:
        return mask_string(_bound(str(value), MAX_DETAIL_VALUE_LEN))
    if isinstance(value, Mapping):
        return _clean_details(value, depth + 1)
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)[:MAX_DETAIL_ITEMS]
        return [_clean_value(item, depth + 1) for item in items]
    return mask_string(_bound(str(value), MAX_DETAIL_VALUE_LEN))


def _clean_details(details: Mapping[Any, Any], depth: int = 0) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in list(details.items())[:MAX_DETAIL_ITEMS]:
        cleaned[_bound(str(key), MAX_DETAIL_KEY_LEN)] = _clean_value(value, depth)
    return cleaned


def _check_line(line: str, prev_hash: str, expected_seq: int) -> str | None:
    """The row hash of a valid line that chains to ``prev_hash``, else ``None``."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    claimed = row.pop(HASH_KEY, None)
    if (
        not isinstance(claimed, str)
        or row.get("prev_hash") != prev_hash
        or row.get("seq") != expected_seq
        or row_hash(prev_hash, row) != claimed
    ):
        return None
    return claimed


def _walk(path: Path) -> tuple[str, int, int | None]:
    """``(last good hash, lines in the file, first bad line)``; a missing file is an empty
    chain. Lines after the first bad one are counted, not verified."""
    if not path.exists():
        return GENESIS_HASH, 0, None
    last_hash = GENESIS_HASH
    rows = 0
    first_bad: int | None = None
    with path.open("r", encoding="utf-8", newline="\n") as handle:
        for number, line in enumerate(handle, start=1):
            rows = number
            if first_bad is not None:
                continue
            next_hash = _check_line(line.rstrip("\n"), last_hash, number)
            if next_hash is None:
                first_bad = number
                continue
            last_hash = next_hash
    return last_hash, rows, first_bad


def verify(path: Path) -> VerifyResult:
    """Recompute the chain of an audit file (``carto-ctl audit verify``)."""
    _last, rows, first_bad = _walk(path)
    return VerifyResult(ok=first_bad is None, rows=rows, first_bad_line=first_bad)


class EdgeAudit:
    """Append rows to the chain. Open verifies the file; ``close`` releases it."""

    __slots__ = ("_handle", "_last_hash", "_lock", "_lock_path", "_path", "_seq", "_size")

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        last_hash, rows, first_bad = _walk(path)
        if first_bad is not None:
            msg = f"audit chain broken at line {first_bad} of {path}"
            raise AuditChainError(msg)
        self._last_hash = last_hash
        self._seq = rows
        self._lock = threading.Lock()
        self._lock_path = path.with_name(path.name + LOCK_SUFFIX)
        self._handle: TextIO = path.open("a", encoding="utf-8", newline="\n")
        self._size = os.fstat(self._handle.fileno()).st_size

    def __repr__(self) -> str:
        return f"EdgeAudit(path={str(self._path)!r}, seq={self._seq})"

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
    def last_hash(self) -> str:
        return self._last_hash

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        action: str,
        actor: str,
        target: str,
        details: Mapping[str, object],
        request_id: str = "",
    ) -> AuditRow:
        """Append one row and return it. ``details`` strings are masked; never pass values."""
        cleaned = _clean_details(details)
        with self._lock, _exclusive(self._lock_path):
            if os.fstat(self._handle.fileno()).st_size != self._size:
                tail = _tail(self._path)  # another process appended since our last row
                if tail is not None:
                    self._seq, self._last_hash = tail
            moment = datetime.now(UTC)
            row: dict[str, object] = {
                "seq": self._seq + 1,
                "ts": moment.isoformat(timespec="milliseconds"),
                "action": _bound(action, MAX_ACTION_LEN),
                "actor": _bound(actor, MAX_ACTOR_LEN),
                "target": _bound(target, MAX_TARGET_LEN),
                "request_id": _bound(request_id, MAX_REQUEST_ID_LEN),
                "details": cleaned,
                "prev_hash": self._last_hash,
            }
            digest = row_hash(self._last_hash, row)
            row[HASH_KEY] = digest
            self._handle.write(canonical_json(row) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._seq += 1
            self._last_hash = digest
            self._size = os.fstat(self._handle.fileno()).st_size
        return AuditRow.model_validate({**row, "ts": moment})

    @staticmethod
    def verify(path: Path) -> VerifyResult:
        return verify(path)

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()
