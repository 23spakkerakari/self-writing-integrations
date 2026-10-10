"""The seam between the receivers and the batching stage (spec 5.4 step 7, 8.5; plan M1 wave 2).

Two kinds of code hand records to the edge: the gateway's push routes (OTLP, webhooks) and the
poll scheduler that drives pull connectors. Both talk to one :class:`IngestorLike`: it runs
the records through the pipeline, writes the vault entries, seals batches and appends them to
the disk buffer, and reports the buffer's backpressure state so a push receiver can answer 429
or 503 and a pull loop can pause (spec 8.5). The implementation lives in
:mod:`carto_edge.pipeline.batch`; this module holds only the contract so the gateway routes can
be tested against a fake.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from carto_edge.pipeline.model import RawRecord

__all__ = ["BackpressureState", "IngestOutcome", "IngestorLike"]


class BackpressureState(StrEnum):
    """What the disk buffer tells the receivers (spec 8.5)."""

    OK = "ok"
    """Accept records."""
    SLOW = "slow"
    """Above the backpressure ratio: pull connectors pause, push receivers answer 429."""
    FULL = "full"
    """At the cap: push receivers answer 503; nothing is appended until core catches up."""


@dataclass(slots=True)
class IngestOutcome:
    """Counts of one :meth:`IngestorLike.ingest` call; never values."""

    records: int = 0
    events: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    vault_entries: int = 0
    batches_sealed: int = 0

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())


@runtime_checkable
class IngestorLike(Protocol):
    """Records in, durably buffered batches out."""

    def ingest(self, records: Iterable[RawRecord]) -> IngestOutcome:
        """Process records; full batches are appended to the buffer before this returns."""
        ...

    def flush(self) -> int:
        """Seal every partial batch into the buffer; returns how many batches were sealed."""
        ...

    def backpressure(self) -> BackpressureState: ...
