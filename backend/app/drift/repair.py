"""Repair pipeline: incident -> triage -> candidate manifest -> verification against the API as
it behaves now -> change request.

The candidate is never verified against a mock generated from itself; that would prove nothing.
It is verified against a World: the live API through the tenant's connection, or in tests and
mock mode a DriftWorld pinned to the published manifest plus the drift scenario. A mechanical
patch is tried first; the repair agent (Claude) is called only when no mechanical fix applies
or the mechanical one fails verification. Credentials never reach the agent.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import anthropic
import httpx
from pydantic import BaseModel

from app.drift.changes import ChangeRequests
from app.drift.models import ChangeRequest, DriftIncident, Triage
from app.drift.monitor import DriftMonitor
from app.drift.patches import deterministic_repair
from app.drift.triage import triage as triage_incident
from app.manifest.schema import IntegrationManifest, load_manifest
from app.registry.store import NotFound, Registry
from app.runtime.gateway import Connection
from app.runtime.secrets import SecretsProvider
from app.synthesis.agent import SynthesisError, synthesize
from app.verification.harness import VerificationReport, verify

_GUIDANCE = {
    "schema": (
        "Update the endpoint's response_schema, items_path and pagination paths to match the observed body, and "
        "repoint mapping sources at renamed or moved fields. Keep every canonical target the record can still populate."
    ),
    "semantic": (
        "A field now carries a value the schema and mapping do not know. Extend the enum in the schema and the "
        "enum_map so the new value maps onto the correct canonical value (employment_status: active, on_leave, "
        "terminated, unknown). Map it to unknown only if its meaning is genuinely unclear."
    ),
    "auth": (
        "The API rejected the credentials. The auth scheme, header name or credential location may have changed; "
        "use the specification and the response body to decide. Reference secrets by secret_ref only; never invent values."
    ),
    "deprecation": (
        "The endpoint was retired. Adopt the successor path from the Link header, the response body or the "
        "specification, and keep the schema and mapping unless the successor's body differs."
    ),
    "behavioral": (
        "Response format, pagination or rate limiting changed. Adjust items_path, pagination settings, the retry "
        "policy or rate_limit to match the observed behavior."
    ),
}


class RepairError(Exception):
    pass


@dataclass
class World:
    """How to reach the API as it behaves now. transport=None means real HTTP."""

    connection: Connection
    secrets: SecretsProvider
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)


class RepairOutcome(BaseModel):
    incident: DriftIncident
    triage: Triage
    change_request: ChangeRequest | None = None
    report: VerificationReport | None = None
    strategy: str | None = None
    rounds: int = 0
    error: str | None = None


class RepairPipeline:
    def __init__(
        self,
        registry: Registry,
        monitor: DriftMonitor,
        changes: ChangeRequests,
        model: str = "claude-opus-5",
        max_attempts: int = 3,
        max_rounds: int = 2,
        repair_fn: Any = synthesize,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.registry = registry
        self.monitor = monitor
        self.changes = changes
        self.model = model
        self.max_attempts = max_attempts
        self.max_rounds = max_rounds
        self.repair_fn = repair_fn
        self.client = client

    # --- steps ---------------------------------------------------------------------------------

    def triage(self, incident_id: int) -> tuple[DriftIncident, Triage]:
        incident = self.monitor.get(incident_id)
        manifest = self._published_manifest(incident.integration)
        result = triage_incident(incident, manifest)
        if result.repairable:
            status = "triaged"
        elif result.drift_class == "cosmetic":
            status = "resolved"
        elif result.drift_class == "transient":
            status = "triaged"
        else:
            status = "needs_human"
        incident = self.monitor.set_triage(incident_id, result, status)  # type: ignore[arg-type]
        return incident, result

    def run(self, incident_id: int, world: World) -> RepairOutcome:
        incident = self.monitor.get(incident_id)
        if incident.status in ("resolved", "dismissed"):
            raise RepairError(f"incident {incident_id} is {incident.status}")
        if incident.change_request_id is not None:
            existing = self.changes.get(incident.change_request_id)
            if existing.status in ("pending", "approved", "canary"):
                return RepairOutcome(incident=incident, triage=_triage_of(incident), change_request=existing, strategy=existing.strategy)

        incident, triage = self.triage(incident_id) if incident.drift_class is None else (incident, _triage_of(incident))
        if not triage.repairable:
            return RepairOutcome(incident=incident, triage=triage)

        name = incident.integration
        try:
            published = self.registry.get_published(name)
        except NotFound as exc:
            incident = self.monitor.set_status(incident_id, "repair_failed", note=str(exc))
            return RepairOutcome(incident=incident, triage=triage, error=str(exc))
        manifest = load_manifest(published.manifest)
        snapshot = self.registry.latest_snapshot(name)
        spec_text = snapshot.content if snapshot is not None else (published.spec_source or "")
        self.monitor.set_status(incident_id, "in_repair")

        candidate: IntegrationManifest | None = None
        report: VerificationReport | None = None
        strategy = ""
        rounds = 0
        feedback = self._feedback(incident, triage)

        patched = deterministic_repair(manifest, incident)
        if patched is not None:
            candidate, strategy = patched[0], f"deterministic:{patched[1]}"
            rounds += 1
            report = self._verify(candidate, world)

        if candidate is None or report is None or not report.passed:
            if candidate is not None and report is not None:
                feedback += f"\n\nA mechanical patch ({strategy}) was tried and still failed verification:\n{_report_feedback(report)}"
            previous = candidate or manifest
            for _ in range(self.max_rounds):
                rounds += 1
                try:
                    result = self.repair_fn(
                        spec_text,
                        name,
                        model=self.model,
                        max_attempts=self.max_attempts,
                        client=self.client,
                        previous=previous,
                        feedback=feedback,
                    )
                except SynthesisError as exc:
                    incident = self.monitor.set_status(incident_id, "repair_failed", note=f"repair agent failed: {exc}")
                    return RepairOutcome(incident=incident, triage=triage, strategy=f"agent:{self.model}", rounds=rounds, error=str(exc))
                candidate, strategy = result.manifest, f"agent:{self.model}"
                report = self._verify(candidate, world)
                if report.passed:
                    break
                feedback = self._feedback(incident, triage) + f"\n\nThe previous repair still failed verification:\n{_report_feedback(report)}"
                previous = candidate

        assert candidate is not None and report is not None
        candidate = candidate.model_copy(update={"name": name, "version": self.registry.next_version(name)})
        report = report.model_copy(update={"integration": name, "version": candidate.version})
        self.registry.create_version(candidate, spec_source=spec_text or None, provenance=f"repair:{strategy}:incident:{incident_id}")
        self.registry.record_verification(name, candidate.version, report.model_dump(mode="json"), report.passed)
        change = self.changes.create(
            integration=name,
            incident_id=incident_id,
            base_version=published.version,
            base_manifest=published.manifest,
            candidate_version=candidate.version,
            candidate_manifest=candidate.model_dump(mode="json"),
            triage=triage,
            strategy=strategy,
            report=report,
        )
        if report.passed:
            incident = self.monitor.set_status(incident_id, "in_repair", change_request_id=change.id)
        else:
            incident = self.monitor.set_status(
                incident_id, "repair_failed", note=f"candidate {candidate.version} failed verification", change_request_id=change.id
            )
        return RepairOutcome(incident=incident, triage=triage, change_request=change, report=report, strategy=strategy, rounds=rounds)

    # --- helpers -----------------------------------------------------------------------------

    def _published_manifest(self, name: str) -> IntegrationManifest | None:
        try:
            return load_manifest(self.registry.get_published(name).manifest)
        except NotFound:
            return None

    @staticmethod
    def _verify(candidate: IntegrationManifest, world: World) -> VerificationReport:
        return verify(
            candidate,
            mode="live",
            connection=world.connection,
            secrets=world.secrets,
            transport=world.transport,
            sleep=world.sleep,
        )

    @staticmethod
    def _feedback(incident: DriftIncident, triage: Triage) -> str:
        lines = [
            f"Drift incident #{incident.id} on endpoint '{incident.endpoint_id}': "
            f"kind={incident.kind}, class={triage.drift_class}, risk={triage.risk_class}.",
            f"Observed {incident.count} time(s) between {_stamp(incident.first_seen)} and {_stamp(incident.last_seen)} "
            f"against manifest version {incident.version}.",
            f"Triage: {triage.rationale}",
            "Observations:",
            *[f"  - {sample}" for sample in incident.samples],
        ]
        if incident.sample_body is not None:
            lines.append("Sample response body as the API returns it now (truncated):")
            lines.append(json.dumps(incident.sample_body, indent=1, default=str)[:6000])
        guidance = _GUIDANCE.get(triage.drift_class)
        if guidance:
            lines.append(guidance)
        return "\n".join(lines)


def _report_feedback(report: VerificationReport) -> str:
    lines: list[str] = []
    for check in report.checks:
        if not check.passed:
            lines.append(f"endpoint '{check.endpoint_id}' (HTTP {check.status_code}):")
            lines.extend(f"  - {e}" for e in check.errors)
    return "\n".join(lines) or "verification failed without endpoint-level errors"


def _triage_of(incident: DriftIncident) -> Triage:
    return Triage(
        drift_class=incident.drift_class or "behavioral",
        risk_class=incident.risk_class or "medium",
        repairable=bool(incident.repairable),
        rationale=incident.triage_note,
    )


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")
