"""Drift monitor: turns gateway DriftEvents and spec re-fetches into incidents.

Incidents aggregate by (integration, endpoint, kind) while unresolved, so a renamed field that
breaks a thousand calls is one incident with a count of a thousand and a handful of distinct
samples, not a thousand rows. Resolving or dismissing closes the incident; a recurrence opens
a new one, which keeps the history honest.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select

from app.db import Database, utcnow
from app.drift.models import DriftIncident, DriftIncidentRow, IncidentStatus, Triage
from app.registry.store import NotFound, Registry
from app.runtime.gateway import CallResult, DriftEvent

ACTIVE_STATUSES = ("open", "triaged", "in_repair", "needs_human", "repair_failed")
_SAMPLE_BODY_CHARS = 8000


class MonitorError(Exception):
    pass


class DriftMonitor:
    def __init__(
        self,
        db: Database,
        registry: Registry,
        clock: Callable[[], datetime] = utcnow,
        max_samples: int = 10,
    ) -> None:
        self.db = db
        self.registry = registry
        self.clock = clock
        self.max_samples = max_samples

    # --- ingestion ----------------------------------------------------------------------

    def ingest(
        self, events: list[DriftEvent], sample_body: Any = None, tenant_id: str | None = None
    ) -> list[DriftIncident]:
        """Fold events into incidents. One call that produced twenty schema violations on one
        endpoint counts as one observation with up to `max_samples` distinct details."""
        if not events:
            return []
        grouped: dict[tuple[str, str, str], list[DriftEvent]] = {}
        for event in events:
            grouped.setdefault((event.integration, event.endpoint_id, event.kind), []).append(event)
        now = self.clock()
        touched: list[DriftIncidentRow] = []
        with self.db.session() as s:
            for (integration, endpoint_id, kind), group in grouped.items():
                row = s.scalar(
                    select(DriftIncidentRow)
                    .where(
                        DriftIncidentRow.integration == integration,
                        DriftIncidentRow.endpoint_id == endpoint_id,
                        DriftIncidentRow.kind == kind,
                        DriftIncidentRow.status.in_(ACTIVE_STATUSES),
                    )
                    .order_by(DriftIncidentRow.id.desc())
                )
                if row is None:
                    row = DriftIncidentRow(
                        integration=integration,
                        endpoint_id=endpoint_id,
                        kind=kind,
                        version=group[0].version,
                        tenant_id=tenant_id,
                        first_seen=now,
                        last_seen=now,
                        count=0,
                        samples_json="[]",
                    )
                    s.add(row)
                samples: list[str] = json.loads(row.samples_json or "[]")
                for event in group:
                    if event.detail not in samples and len(samples) < self.max_samples:
                        samples.append(event.detail)
                    if event.status_code is not None:
                        row.status_code = event.status_code
                row.samples_json = json.dumps(samples)
                row.count += 1
                row.last_seen = now
                if tenant_id and not row.tenant_id:
                    row.tenant_id = tenant_id
                if sample_body is not None:
                    row.sample_body_json = _truncate_json(sample_body)
                touched.append(row)
            s.commit()
            for row in touched:
                s.refresh(row)
            return [DriftIncident.from_row(r) for r in touched]

    def ingest_result(self, result: CallResult, tenant_id: str | None = None) -> list[DriftIncident]:
        return self.ingest(result.drift_events, sample_body=result.raw_first_page, tenant_id=tenant_id)

    def check_spec(self, name: str, spec_text: str) -> DriftIncident | None:
        """Scheduled spec re-fetch: snapshot the text and open a spec_changed incident when it
        differs from the previous snapshot. The first snapshot only establishes a baseline."""
        latest = self.registry.latest_snapshot(name)
        digest = hashlib.sha256(spec_text.encode("utf-8")).hexdigest()
        if latest is not None and latest.content_hash == digest:
            return None
        self.registry.save_snapshot(name, spec_text)
        if latest is None:
            return None
        diff = list(
            difflib.unified_diff(
                latest.content.splitlines(), spec_text.splitlines(), "previous", "current", lineterm="", n=1
            )
        )
        try:
            version = self.registry.get_published(name).version
        except NotFound:
            version = "unpublished"
        changed = sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---")))
        event = DriftEvent(
            integration=name,
            version=version,
            endpoint_id="*",
            kind="spec_changed",
            detail=f"specification changed: {changed} changed line(s) since {latest.fetched_at:%Y-%m-%d %H:%M}",
        )
        incidents = self.ingest([event], sample_body={"diff": diff[:400]})
        return incidents[0] if incidents else None

    # --- reads ----------------------------------------------------------------------------

    def get(self, incident_id: int) -> DriftIncident:
        with self.db.session() as s:
            row = s.get(DriftIncidentRow, incident_id)
            if row is None:
                raise NotFound(f"incident {incident_id} not found")
            return DriftIncident.from_row(row)

    def list(
        self, integration: str | None = None, status: IncidentStatus | None = None, active_only: bool = False
    ) -> list[DriftIncident]:
        with self.db.session() as s:
            query = select(DriftIncidentRow).order_by(DriftIncidentRow.id)
            if integration:
                query = query.where(DriftIncidentRow.integration == integration)
            if status:
                query = query.where(DriftIncidentRow.status == status)
            if active_only:
                query = query.where(DriftIncidentRow.status.in_(ACTIVE_STATUSES))
            return [DriftIncident.from_row(r) for r in s.scalars(query)]

    # --- state changes ----------------------------------------------------------------------

    def set_triage(self, incident_id: int, triage: Triage, status: IncidentStatus) -> DriftIncident:
        with self.db.session() as s:
            row = self._row(s, incident_id)
            row.drift_class = triage.drift_class
            row.risk_class = triage.risk_class
            row.repairable = triage.repairable
            row.triage_note = triage.rationale
            row.status = status
            if status == "resolved":
                row.resolved_at = self.clock()
            s.commit()
            s.refresh(row)
            return DriftIncident.from_row(row)

    def set_status(
        self, incident_id: int, status: IncidentStatus, note: str | None = None, change_request_id: int | None = None
    ) -> DriftIncident:
        with self.db.session() as s:
            row = self._row(s, incident_id)
            row.status = status
            if note is not None:
                row.note = note
            if change_request_id is not None:
                row.change_request_id = change_request_id
            if status in ("resolved", "dismissed"):
                row.resolved_at = self.clock()
            s.commit()
            s.refresh(row)
            return DriftIncident.from_row(row)

    def dismiss(self, incident_id: int, actor: str, note: str = "") -> DriftIncident:
        return self.set_status(incident_id, "dismissed", note=f"dismissed by {actor}: {note}".rstrip(": "))

    def resolve(self, incident_id: int, note: str = "") -> DriftIncident:
        return self.set_status(incident_id, "resolved", note=note or None)

    @staticmethod
    def _row(s: Any, incident_id: int) -> DriftIncidentRow:
        row = s.get(DriftIncidentRow, incident_id)
        if row is None:
            raise NotFound(f"incident {incident_id} not found")
        return row


def _truncate_json(value: Any) -> str:
    text = json.dumps(value, default=str)
    if len(text) <= _SAMPLE_BODY_CHARS:
        return text
    # Keep the sample parseable: trim lists first, then fall back to a string excerpt.
    if isinstance(value, list):
        return _truncate_json(value[: max(1, len(value) // 2)])
    if isinstance(value, dict):
        trimmed = {k: (v[:2] if isinstance(v, list) else v) for k, v in value.items()}
        text = json.dumps(trimmed, default=str)
        if len(text) <= _SAMPLE_BODY_CHARS:
            return text
    return json.dumps({"_truncated": text[:_SAMPLE_BODY_CHARS]})
