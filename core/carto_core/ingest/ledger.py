"""Batch ledger: ingest idempotency by batch id (ADR 0015, spec 8.5).

``events`` and ``event_identifiers`` stay plain ``MergeTree`` tables (spec 7.2), so the write
path itself does not deduplicate. ``ingest-api`` and the bundle loader keep ``ingest_batches
(tenant_id, batch_id)`` in PostgreSQL instead: a batch already in the ledger is acknowledged and
not rewritten. The order of operations is ledger lookup, ClickHouse write, ledger insert, so a
failed write leaves no ledger row and the edge's retry is taken, while a failed ledger insert
after a successful write is answered 503 and the retry writes the batch a second time, which the
linker tolerates (ADR 0015: it counts distinct tokens).

Every statement is parameterized (spec 14.7). Errors raise :class:`LedgerError` naming the
driver error class only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.db.postgres import ping_engine

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "BatchLedger",
    "InMemoryBatchLedger",
    "LedgerEntry",
    "LedgerError",
    "PostgresBatchLedger",
]


class LedgerError(Exception):
    """The ledger could not be read or written; the caller answers 503 so the edge retries."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One accepted batch (one row of ``ingest_batches``)."""

    tenant_id: str
    batch_id: str
    source_id: str
    event_count: int
    received_at: datetime


@runtime_checkable
class BatchLedger(Protocol):
    """What the ingest path and the bundle loader need from the ledger."""

    def contains(self, tenant_id: str, batch_id: str) -> bool: ...

    def record(self, entry: LedgerEntry) -> bool:
        """Insert the entry; False when another delivery recorded the same batch first."""
        ...

    def ping(self) -> bool: ...


class InMemoryBatchLedger:
    """A ledger in a dict, for tests and the offline smoke path."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        self._lock = threading.Lock()

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        with self._lock:
            return tuple(self._entries.values())

    def contains(self, tenant_id: str, batch_id: str) -> bool:
        with self._lock:
            return (tenant_id, batch_id) in self._entries

    def record(self, entry: LedgerEntry) -> bool:
        key = (entry.tenant_id, entry.batch_id)
        with self._lock:
            if key in self._entries:
                return False
            self._entries[key] = entry
            return True

    def ping(self) -> bool:
        return True


_SELECT = sqlalchemy.text(
    "SELECT 1 FROM ingest_batches WHERE tenant_id = :tenant_id AND batch_id = :batch_id"
)
_INSERT = sqlalchemy.text(
    "INSERT INTO ingest_batches "
    "(tenant_id, batch_id, source_id, received_at, event_count, created_at, updated_at) "
    "VALUES (:tenant_id, :batch_id, :source_id, :received_at, :event_count, "
    ":received_at, :received_at) "
    "ON CONFLICT (tenant_id, batch_id) DO NOTHING"
)


class PostgresBatchLedger:
    """The ``ingest_batches`` table (migration 0001)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def contains(self, tenant_id: str, batch_id: str) -> bool:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    _SELECT, {"tenant_id": tenant_id, "batch_id": batch_id}
                ).first()
        except SQLAlchemyError as exc:
            msg = f"ledger lookup failed: {type(exc).__name__}"
            raise LedgerError(msg) from exc
        return row is not None

    def record(self, entry: LedgerEntry) -> bool:
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    _INSERT,
                    {
                        "tenant_id": entry.tenant_id,
                        "batch_id": entry.batch_id,
                        "source_id": entry.source_id,
                        "received_at": entry.received_at,
                        "event_count": entry.event_count,
                    },
                )
        except SQLAlchemyError as exc:
            msg = f"ledger insert failed: {type(exc).__name__}"
            raise LedgerError(msg) from exc
        return result.rowcount == 1

    def ping(self) -> bool:
        return ping_engine(self._engine)
