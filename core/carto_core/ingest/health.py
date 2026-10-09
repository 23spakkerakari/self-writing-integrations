"""Source health as reported by edge heartbeats (spec 8.1 common requirements, 8.5, 12).

``POST /internal/heartbeat`` upserts one :class:`SourceHealthRecord` per ``(tenant_id,
source_id)`` into ``source_health`` (migration 0001). The detector (M4) reads it to tell "no
data" from "nothing happened"; ``GET /sources/{id}/health`` (M2) shows it. ``message`` is stored
as the edge sent it; the edge redacts it first (spec 2.3 invariant 7).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.db.postgres import ping_engine

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from carto_schema.ingest import SourceHeartbeat

__all__ = [
    "HealthStoreError",
    "InMemorySourceHealthStore",
    "PostgresSourceHealthStore",
    "SourceHealthRecord",
    "SourceHealthStore",
]


class HealthStoreError(Exception):
    """The health store could not be written; the caller answers 503."""


@dataclass(frozen=True, slots=True)
class SourceHealthRecord:
    """One row of ``source_health``: the latest heartbeat of a source."""

    tenant_id: str
    source_id: str
    status: str
    last_success_at: datetime | None
    lag_seconds: float
    error_count: int
    buffer_depth: int
    oldest_buffered_at: datetime | None
    message: str
    received_at: datetime

    @classmethod
    def from_heartbeat(cls, heartbeat: SourceHeartbeat, received_at: datetime) -> Self:
        return cls(
            tenant_id=heartbeat.tenant_id,
            source_id=heartbeat.source_id,
            status=heartbeat.status.value,
            last_success_at=heartbeat.last_success_at,
            lag_seconds=heartbeat.lag_seconds,
            error_count=heartbeat.error_count,
            buffer_depth=heartbeat.buffer_depth,
            oldest_buffered_at=heartbeat.oldest_buffered_at,
            message=heartbeat.message,
            received_at=received_at,
        )


@runtime_checkable
class SourceHealthStore(Protocol):
    def upsert(self, record: SourceHealthRecord) -> None: ...

    def get(self, tenant_id: str, source_id: str) -> SourceHealthRecord | None: ...

    def ping(self) -> bool: ...


class InMemorySourceHealthStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], SourceHealthRecord] = {}
        self._lock = threading.Lock()

    def upsert(self, record: SourceHealthRecord) -> None:
        with self._lock:
            self._records[(record.tenant_id, record.source_id)] = record

    def get(self, tenant_id: str, source_id: str) -> SourceHealthRecord | None:
        with self._lock:
            return self._records.get((tenant_id, source_id))

    def ping(self) -> bool:
        return True


_UPSERT = sqlalchemy.text(
    "INSERT INTO source_health (tenant_id, source_id, status, last_success_at, lag_seconds, "
    "error_count, buffer_depth, oldest_buffered_at, message, received_at, created_at, "
    "updated_at) VALUES (:tenant_id, :source_id, :status, :last_success_at, :lag_seconds, "
    ":error_count, :buffer_depth, :oldest_buffered_at, :message, :received_at, :received_at, "
    ":received_at) ON CONFLICT (tenant_id, source_id) DO UPDATE SET "
    "status = EXCLUDED.status, last_success_at = EXCLUDED.last_success_at, "
    "lag_seconds = EXCLUDED.lag_seconds, error_count = EXCLUDED.error_count, "
    "buffer_depth = EXCLUDED.buffer_depth, oldest_buffered_at = EXCLUDED.oldest_buffered_at, "
    "message = EXCLUDED.message, received_at = EXCLUDED.received_at, "
    "updated_at = EXCLUDED.received_at"
)
_SELECT = sqlalchemy.text(
    "SELECT tenant_id, source_id, status, last_success_at, lag_seconds, error_count, "
    "buffer_depth, oldest_buffered_at, message, received_at FROM source_health "
    "WHERE tenant_id = :tenant_id AND source_id = :source_id"
)


class PostgresSourceHealthStore:
    """The ``source_health`` table (migration 0001)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(self, record: SourceHealthRecord) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    _UPSERT,
                    {
                        "tenant_id": record.tenant_id,
                        "source_id": record.source_id,
                        "status": record.status,
                        "last_success_at": record.last_success_at,
                        "lag_seconds": record.lag_seconds,
                        "error_count": record.error_count,
                        "buffer_depth": record.buffer_depth,
                        "oldest_buffered_at": record.oldest_buffered_at,
                        "message": record.message,
                        "received_at": record.received_at,
                    },
                )
        except SQLAlchemyError as exc:
            msg = f"source health upsert failed: {type(exc).__name__}"
            raise HealthStoreError(msg) from exc

    def get(self, tenant_id: str, source_id: str) -> SourceHealthRecord | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    _SELECT, {"tenant_id": tenant_id, "source_id": source_id}
                ).first()
        except SQLAlchemyError as exc:
            msg = f"source health lookup failed: {type(exc).__name__}"
            raise HealthStoreError(msg) from exc
        if row is None:
            return None
        return SourceHealthRecord(
            tenant_id=row.tenant_id,
            source_id=row.source_id,
            status=row.status,
            last_success_at=row.last_success_at,
            lag_seconds=float(row.lag_seconds),
            error_count=int(row.error_count),
            buffer_depth=int(row.buffer_depth),
            oldest_buffered_at=row.oldest_buffered_at,
            message=row.message,
            received_at=row.received_at,
        )

    def ping(self) -> bool:
        return ping_engine(self._engine)
