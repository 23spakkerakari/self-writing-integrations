"""ClickHouse client factory and the event writer (spec 5.3 ``ingest-api``, 7.2, 14.4).

The writer turns :class:`~carto_schema.event.CanonicalEvent` objects into one ``events`` row and
one ``event_identifiers`` row per identifier form (spec 7.2 "exploded identifier forms, the join
workhorse"), with ``field_ref = system_id/template_id/field`` (spec 7.2, ADR 0016). Rows are
built by pure functions so the mapping is unit-tested without a server; inserts are
column-oriented, which is what clickhouse-connect sends most cheaply. ``template_text`` is not a
column of ``events`` (spec 7.2) and is not written here; templates live in PostgreSQL (spec 7.3)
from M2 on.

A failed insert raises :class:`WriterError` naming the error class only, never the rows, so the
caller can answer 503 and the edge retries the batch (ADR 0015).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

    from carto_core.settings import ClickHouseSettings
    from carto_schema.event import CanonicalEvent

__all__ = [
    "EVENTS_TABLE",
    "EVENT_COLUMNS",
    "EVENT_IDENTIFIERS_TABLE",
    "IDENTIFIER_COLUMNS",
    "ClickHouseWriter",
    "EventRow",
    "EventWriter",
    "IdentifierRow",
    "WriteResult",
    "WriterError",
    "columns_of",
    "create_client",
    "event_row",
    "field_ref",
    "identifier_rows",
]

EVENTS_TABLE: Final = "events"
EVENT_IDENTIFIERS_TABLE: Final = "event_identifiers"

EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "event_id",
    "source_id",
    "system_id",
    "kind",
    "observed_at",
    "ingested_at",
    "observed_at_quality",
    "template_id",
    "severity",
    "attributes",
    "actor_token",
    "actor_kind",
)
"""``events`` columns in DDL order (spec 7.2, ``core/migrations/clickhouse/0001_events.sql``)."""

IDENTIFIER_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "token",
    "field_ref",
    "form",
    "shape",
    "event_id",
    "system_id",
    "observed_at",
)
"""``event_identifiers`` columns in DDL order (spec 7.2, ``0002_event_identifiers.sql``)."""

type EventRow = tuple[
    str,
    str,
    str,
    str,
    str,
    datetime,
    datetime,
    str,
    str,
    str | None,
    dict[str, str],
    str | None,
    str | None,
]
"""One ``events`` row, in :data:`EVENT_COLUMNS` order."""

type IdentifierRow = tuple[str, str, str, str, str, str, str, datetime]
"""One ``event_identifiers`` row, in :data:`IDENTIFIER_COLUMNS` order."""


class WriterError(Exception):
    """ClickHouse did not take the write. The message names the cause, never the rows."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    """How many rows one call wrote."""

    events: int
    identifiers: int


@runtime_checkable
class EventWriter(Protocol):
    """What ``ingest-api`` and the bundle loader write through."""

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult: ...

    def ping(self) -> bool: ...


def field_ref(system_id: str, template_id: str, field: str) -> str:
    """``system_id/template_id/field`` (spec 7.2 comment on ``field_ref``, ADR 0016)."""
    return f"{system_id}/{template_id}/{field}"


def event_row(event: CanonicalEvent) -> EventRow:
    """The ``events`` row of one canonical event (spec 7.1 to 7.2 mapping)."""
    actor = event.actor
    return (
        event.tenant_id,
        event.event_id,
        event.source_id,
        event.system_id,
        event.kind.value,
        event.observed_at,
        event.ingested_at,
        event.observed_at_quality.value,
        event.template_id,
        event.severity.value if event.severity is not None else None,
        dict(event.attributes),
        actor.token if actor is not None else None,
        actor.kind.value if actor is not None else None,
    )


def identifier_rows(event: CanonicalEvent) -> list[IdentifierRow]:
    """One ``event_identifiers`` row per identifier form of the event (spec 7.2)."""
    return [
        (
            event.tenant_id,
            identifier.token,
            field_ref(event.system_id, event.template_id, identifier.field),
            identifier.form,
            identifier.shape,
            event.event_id,
            event.system_id,
            event.observed_at,
        )
        for identifier in event.identifiers
    ]


def columns_of(rows: Sequence[Sequence[Any]], width: int) -> list[list[Any]]:
    """Transpose rows into ``width`` column lists (clickhouse-connect ``column_oriented``)."""
    columns: list[list[Any]] = [[] for _ in range(width)]
    for row in rows:
        for index, value in enumerate(row):
            columns[index].append(value)
    return columns


class ClickHouseWriter:
    """Writes canonical events to ``events`` and ``event_identifiers`` (spec 5.4 step 8)."""

    __slots__ = ("_client",)

    def __init__(self, client: Client) -> None:
        self._client = client

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        """Insert every event and its identifier forms. Raises :class:`WriterError` on failure."""
        if not events:
            return WriteResult(events=0, identifiers=0)
        rows = [event_row(event) for event in events]
        id_rows: list[IdentifierRow] = []
        for event in events:
            id_rows.extend(identifier_rows(event))
        try:
            self._client.insert(
                EVENTS_TABLE,
                columns_of(rows, len(EVENT_COLUMNS)),
                column_names=list(EVENT_COLUMNS),
                column_oriented=True,
            )
            if id_rows:
                self._client.insert(
                    EVENT_IDENTIFIERS_TABLE,
                    columns_of(id_rows, len(IDENTIFIER_COLUMNS)),
                    column_names=list(IDENTIFIER_COLUMNS),
                    column_oriented=True,
                )
        except (ClickHouseError, OSError) as exc:
            msg = f"ClickHouse insert failed: {type(exc).__name__}"
            raise WriterError(msg) from exc
        return WriteResult(events=len(rows), identifiers=len(id_rows))

    def ping(self) -> bool:
        """True when the server answers; never raises (readiness probes call this)."""
        try:
            return bool(self._client.ping())
        except (ClickHouseError, OSError):
            return False


def create_client(settings: ClickHouseSettings) -> Client:
    """Connect with the configured user; the password comes from ``password_file`` (spec 14.4).

    Raises :class:`WriterError` when the server cannot be reached or refuses the login; the
    message never includes the password.
    """
    timeout = max(1, int(settings.timeout_seconds))
    try:
        return clickhouse_connect.get_client(
            host=settings.host,
            port=settings.port,
            username=settings.user,
            password=settings.read_password(),
            database=settings.database,
            secure=settings.secure,
            ca_cert=str(settings.ca_file) if settings.ca_file is not None else None,
            connect_timeout=timeout,
            send_receive_timeout=timeout,
        )
    except (ClickHouseError, OSError) as exc:
        msg = (
            f"cannot connect to ClickHouse at {settings.url} as {settings.user}: "
            f"{type(exc).__name__}"
        )
        raise WriterError(msg) from exc
