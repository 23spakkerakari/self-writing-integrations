"""Edge-to-core envelopes: ingest batches and source heartbeats (spec 8.1, 8.5, 12).

``POST /internal/ingest`` carries one :class:`IngestBatch`; ``POST /internal/heartbeat`` carries
one :class:`SourceHeartbeat`. Both travel over mutual TLS between the edge and core only (spec 12,
"Internal"). The batch is idempotent by ``event_id`` (spec 8.5); the heartbeat lets the detector
tell "no data" from "nothing happened" (spec 8.1, common requirements).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, model_validator

from carto_schema.event import (
    CanonicalEvent,
    ContractModel,
    SchemaVersion,
    SourceId,
    TenantId,
    Ulid,
    UtcTimestamp,
)

MAX_EVENTS_PER_BATCH: Final = 5000
"""Spec 8.5: batches of up to 5,000 events (the 5 MB cap is enforced by the transport)."""

MAX_HEARTBEAT_MESSAGE_LEN: Final = 1024


class SourceStatus(StrEnum):
    """Connector health as the edge sees it (spec 8.1, common requirements)."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"
    PAUSED = "paused"


class IngestBatch(ContractModel):
    """One ``POST /internal/ingest`` body (spec 8.5, 12).

    Every event belongs to the batch's tenant and source; core relies on that to authorize the
    batch once rather than per event.
    """

    schema_version: SchemaVersion
    tenant_id: TenantId
    source_id: SourceId
    batch_id: Ulid
    sent_at: UtcTimestamp
    events: Annotated[list[CanonicalEvent], Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)]

    @model_validator(mode="after")
    def _events_belong_to_batch(self) -> Self:
        for index, event in enumerate(self.events):
            if event.tenant_id != self.tenant_id or event.source_id != self.source_id:
                msg = (
                    f"events[{index}] belongs to tenant {event.tenant_id!r} and source "
                    f"{event.source_id!r}, batch is for {self.tenant_id!r} and {self.source_id!r}"
                )
                raise ValueError(msg)
        return self


class SourceHeartbeat(ContractModel):
    """One ``POST /internal/heartbeat`` body (spec 8.1 common requirements, 8.5, 12).

    ``lag_seconds`` is how far the source cursor trails the source clock; ``buffer_depth`` and
    ``oldest_buffered_at`` describe the edge's disk buffer (spec 8.5 delivery metrics). The
    numeric fields are strict, as in the exported schema: booleans and numeric strings are
    rejected; ``lag_seconds`` accepts an integer. ``message`` is operator-facing free text that
    core stores as sent, so the edge applies its log redaction to it first (spec 2.3
    invariant 7: no secrets, connection strings or raw identifier values).
    """

    schema_version: SchemaVersion
    tenant_id: TenantId
    source_id: SourceId
    sent_at: UtcTimestamp
    status: SourceStatus
    last_success_at: UtcTimestamp | None
    lag_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
    error_count: Annotated[int, Field(ge=0, strict=True)]
    buffer_depth: Annotated[int, Field(ge=0, strict=True)]
    oldest_buffered_at: UtcTimestamp | None
    message: Annotated[str, Field(max_length=MAX_HEARTBEAT_MESSAGE_LEN)] = ""
