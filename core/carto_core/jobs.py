"""Worker jobs: PostgreSQL leases, no broker (spec 5.3, 7.3 ``jobs``).

A :class:`Job` row is leased with ``SELECT ... FOR UPDATE SKIP LOCKED`` by ``carto-core worker``
(:mod:`carto_core.worker`); a lease older than the worker's ``lease_seconds`` is retaken, so a
crashed worker never strands a job. Recurring jobs (``profile`` every five minutes, ``link``
after each profile pass and nightly in full) are re-queued by the worker when they complete
(``run_after`` set to the next slot), so the table always holds at most one pending job per
recurring kind and payload.

This module holds the model and the Protocol; the implementations (:class:`InMemoryJobQueue`
for tests, ``PostgresJobQueue`` in :mod:`carto_core.jobs_postgres`) are package A.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = ["Job", "JobKind", "JobQueue", "JobQueueError"]


class JobQueueError(Exception):
    """The job table could not be read or written."""


class JobKind(StrEnum):
    PROFILE = "profile"
    """Incremental profile pass by store watermark; ``payload['full']`` for the nightly rebuild."""
    LINK = "link"
    """Link pass over the current sketches; ``payload['full']`` for the nightly full pass."""
    FAMILIES = "families"
    """Rebuild entity families after a review decision."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    tenant_id: str
    kind: JobKind
    payload: dict[str, Any] = field(default_factory=dict)
    run_after: datetime | None = None
    leased_until: datetime | None = None
    attempts: int = 0
    last_error: str | None = None


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(
        self,
        tenant_id: str,
        kind: JobKind,
        payload: dict[str, Any],
        run_after: datetime,
        *,
        dedupe: bool = True,
    ) -> Job | None:
        """Add a job; with ``dedupe`` do nothing when an unleased job of the same tenant, kind
        and payload is already waiting (return ``None``)."""
        ...

    def lease(self, kinds: frozenset[JobKind], now: datetime, lease_seconds: int) -> Job | None:
        """Take the oldest runnable job of the kinds (``run_after <= now`` and not leased, or
        its lease expired), mark it leased until ``now + lease_seconds``, increment attempts."""
        ...

    def complete(self, job_id: int) -> None:
        """Delete the job."""
        ...

    def fail(self, job_id: int, error: str, retry_at: datetime | None) -> None:
        """Release the lease; schedule a retry at ``retry_at`` or, when ``None``, delete the job
        (attempts exhausted). ``error`` is a bounded class name, never a message with data."""
        ...

    def pending(self, tenant_id: str) -> list[Job]:
        """Every job of the tenant, for the worker's status output and tests."""
        ...

    def ping(self) -> bool: ...
