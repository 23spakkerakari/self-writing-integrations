"""Change requests: approval gate, canary routing and promotion.

A change request is opened for every verified candidate the repair pipeline produces. It waits
for a human unless the integration's approval policy allows its risk class through. Once
approved, a canary routes a fraction of the integration's calls to the candidate and records
the outcome of every call per arm; promotion publishes the candidate only when the candidate
arm is at least as healthy as the base arm. Rollback is one step and never re-publishes a
version that was rolled back.
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select

from app.db import Database, utcnow
from app.drift.diff import manifest_diff
from app.drift.models import (
    ApprovalPolicy,
    ApprovalPolicyRow,
    ArmStats,
    CanaryArm,
    CanaryObservationRow,
    CanaryReport,
    ChangeRequest,
    ChangeRequestRow,
    ChangeStatus,
    RiskClass,
    Triage,
)
from app.drift.monitor import DriftMonitor
from app.registry.store import NotFound, Registry, RegistryError, VersionRecord
from app.runtime.gateway import CallResult
from app.verification.harness import VerificationReport


class ChangeError(Exception):
    pass


class ChangeRequests:
    def __init__(
        self,
        db: Database,
        registry: Registry,
        monitor: DriftMonitor,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        min_calls: int = 5,
        max_candidate_failure_rate: float = 0.1,
    ) -> None:
        self.db = db
        self.registry = registry
        self.monitor = monitor
        self.clock = clock
        self.rng = rng or random.Random()
        self.min_calls = min_calls
        self.max_candidate_failure_rate = max_candidate_failure_rate

    # --- creation and approval ------------------------------------------------------------

    def create(
        self,
        integration: str,
        incident_id: int | None,
        base_version: str,
        base_manifest: dict[str, Any],
        candidate_version: str,
        candidate_manifest: dict[str, Any],
        triage: Triage,
        strategy: str,
        report: VerificationReport,
    ) -> ChangeRequest:
        diff = manifest_diff(base_manifest, candidate_manifest)
        diff = [op for op in diff if op["path"] != "/version"]
        now = self.clock()
        row = ChangeRequestRow(
            integration=integration,
            incident_id=incident_id,
            base_version=base_version,
            candidate_version=candidate_version,
            drift_class=triage.drift_class,
            risk_class=triage.risk_class,
            strategy=strategy,
            diff_json=json.dumps(diff, default=str),
            verification_json=json.dumps(report.model_dump(mode="json"), default=str),
            verified=report.passed,
            status="pending" if report.passed else "failed",
            created_at=now,
        )
        if report.passed and self.auto_approves(integration, triage.risk_class):
            row.status = "approved"
            row.auto_approved = True
            row.decided_by = "policy"
            row.decided_at = now
            row.decision_note = f"auto-approved: policy for '{integration}' allows {triage.risk_class}-risk changes"
        with self.db.session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
            return ChangeRequest.from_row(row)

    def approve(self, change_id: int, actor: str, note: str = "") -> ChangeRequest:
        return self._decide(change_id, "approved", actor, note, allowed_from=("pending",))

    def reject(self, change_id: int, actor: str, note: str = "") -> ChangeRequest:
        change = self._decide(change_id, "rejected", actor, note, allowed_from=("pending", "approved", "canary"))
        if change.incident_id is not None:
            self.monitor.set_status(change.incident_id, "needs_human", note=f"change request {change.id} rejected by {actor}: {note}")
        return change

    def _decide(self, change_id: int, status: ChangeStatus, actor: str, note: str, allowed_from: tuple[str, ...]) -> ChangeRequest:
        with self.db.session() as s:
            row = self._row(s, change_id)
            if row.status not in allowed_from:
                raise ChangeError(f"change request {change_id} is '{row.status}'; cannot move it to '{status}'")
            row.status = status
            row.decided_by = actor
            row.decided_at = self.clock()
            row.decision_note = note
            s.commit()
            s.refresh(row)
            return ChangeRequest.from_row(row)

    # --- approval policy -----------------------------------------------------------------------

    def get_policy(self, integration: str) -> ApprovalPolicy:
        policy = ApprovalPolicy(integration=integration)
        with self.db.session() as s:
            for row in s.scalars(select(ApprovalPolicyRow).where(ApprovalPolicyRow.integration == integration)):
                policy.auto_approve[row.risk_class] = row.auto_approve  # type: ignore[index]
        return policy

    def set_policy(self, integration: str, risk_class: RiskClass, auto_approve: bool, actor: str = "system") -> ApprovalPolicy:
        with self.db.session() as s:
            row = s.get(ApprovalPolicyRow, (integration, risk_class))
            if row is None:
                row = ApprovalPolicyRow(integration=integration, risk_class=risk_class)
                s.add(row)
            row.auto_approve = auto_approve
            row.updated_by = actor
            s.commit()
        return self.get_policy(integration)

    def auto_approves(self, integration: str, risk_class: str) -> bool:
        return bool(self.get_policy(integration).auto_approve.get(risk_class, False))  # type: ignore[arg-type]

    # --- canary --------------------------------------------------------------------------------

    def start_canary(self, change_id: int, fraction: float) -> ChangeRequest:
        if not 0.0 < fraction <= 1.0:
            raise ChangeError("canary fraction must be in (0, 1]")
        with self.db.session() as s:
            row = self._row(s, change_id)
            if row.status != "approved":
                raise ChangeError(f"change request {change_id} is '{row.status}'; only approved changes can start a canary")
            other = s.scalar(
                select(ChangeRequestRow).where(
                    ChangeRequestRow.integration == row.integration,
                    ChangeRequestRow.status == "canary",
                    ChangeRequestRow.id != row.id,
                )
            )
            if other is not None:
                raise ChangeError(f"change request {other.id} is already in canary for '{row.integration}'")
            row.status = "canary"
            row.canary_fraction = fraction
            row.canary_started_at = self.clock()
            s.commit()
            s.refresh(row)
            return ChangeRequest.from_row(row)

    def active_canary(self, integration: str) -> ChangeRequest | None:
        with self.db.session() as s:
            row = s.scalar(
                select(ChangeRequestRow).where(ChangeRequestRow.integration == integration, ChangeRequestRow.status == "canary")
            )
            return ChangeRequest.from_row(row) if row is not None else None

    def select_version(self, integration: str) -> tuple[VersionRecord, ChangeRequest | None, CanaryArm]:
        """The version a call should run against: the candidate for a `fraction` of calls while a
        canary is active, the published version otherwise."""
        published = self.registry.get_published(integration)
        canary = self.active_canary(integration)
        if canary is None:
            return published, None, "base"
        if self.rng.random() < (canary.canary_fraction or 0.0):
            return self.registry.get_version(integration, canary.candidate_version), canary, "candidate"
        return published, canary, "base"

    def record_call(self, change_id: int, arm: CanaryArm, version: str, result: CallResult) -> None:
        with self.db.session() as s:
            s.add(
                CanaryObservationRow(
                    change_request_id=change_id,
                    arm=arm,
                    version=version,
                    endpoint_id=result.endpoint_id,
                    ok=result.ok,
                    validation_errors=len(result.validation_errors),
                    drift_events=len(result.drift_events),
                    at=self.clock(),
                )
            )
            s.commit()

    def canary_report(self, change_id: int) -> CanaryReport:
        change = self.get(change_id)
        base = ArmStats(version=change.base_version)
        candidate = ArmStats(version=change.candidate_version)
        with self.db.session() as s:
            for obs in s.scalars(select(CanaryObservationRow).where(CanaryObservationRow.change_request_id == change_id)):
                arm = candidate if obs.arm == "candidate" else base
                arm.calls += 1
                arm.failures += 0 if obs.ok else 1
                arm.validation_errors += obs.validation_errors
                arm.drift_events += obs.drift_events
        verdict, reason = self._judge(base, candidate)
        return CanaryReport(
            change_request_id=change_id,
            fraction=change.canary_fraction or 0.0,
            min_calls=self.min_calls,
            base=base,
            candidate=candidate,
            verdict=verdict,
            reason=reason,
        )

    def _judge(self, base: ArmStats, candidate: ArmStats) -> tuple[str, str]:
        if candidate.calls < self.min_calls:
            return "insufficient", f"{candidate.calls}/{self.min_calls} candidate calls observed"
        cand_rate, base_rate = candidate.failure_rate, base.failure_rate
        if cand_rate > self.max_candidate_failure_rate:
            return "fail", f"candidate failure rate {cand_rate:.0%} exceeds {self.max_candidate_failure_rate:.0%}"
        if base.calls >= self.min_calls and cand_rate > base_rate:
            return "fail", f"candidate failure rate {cand_rate:.0%} is worse than base {base_rate:.0%}"
        return "pass", f"candidate failure rate {cand_rate:.0%}" + (f" vs base {base_rate:.0%}" if base.calls else "")

    def abort_canary(self, change_id: int, actor: str, note: str = "") -> ChangeRequest:
        report = self.canary_report(change_id)
        with self.db.session() as s:
            row = self._row(s, change_id)
            if row.status != "canary":
                raise ChangeError(f"change request {change_id} is '{row.status}'; only a running canary can be aborted")
            row.status = "aborted"
            row.canary_json = json.dumps(report.model_dump(mode="json"), default=str)
            row.decision_note = f"canary aborted by {actor}: {note or report.reason}"
            s.commit()
            s.refresh(row)
            change = ChangeRequest.from_row(row)
        if change.incident_id is not None:
            self.monitor.set_status(change.incident_id, "repair_failed", note=change.decision_note)
        return change

    # --- promotion and rollback ------------------------------------------------------------------

    def promote(self, change_id: int, actor: str, force: bool = False) -> ChangeRequest:
        change = self.get(change_id)
        if change.status not in ("approved", "canary"):
            raise ChangeError(f"change request {change_id} is '{change.status}'; only approved or canary changes can be promoted")
        report = self.canary_report(change_id) if change.status == "canary" else None
        if report is not None and report.verdict != "pass" and not force:
            raise ChangeError(f"canary verdict is '{report.verdict}': {report.reason}")
        try:
            self.registry.publish(change.integration, change.candidate_version)
        except RegistryError as exc:
            raise ChangeError(str(exc)) from exc
        with self.db.session() as s:
            row = self._row(s, change_id)
            row.status = "promoted"
            row.promoted_at = self.clock()
            if report is not None:
                row.canary_json = json.dumps(report.model_dump(mode="json"), default=str)
            if row.decided_by is None:
                row.decided_by = actor
                row.decided_at = self.clock()
            row.decision_note = (row.decision_note + "; " if row.decision_note else "") + f"promoted by {actor}"
            s.commit()
            s.refresh(row)
            change = ChangeRequest.from_row(row)
        if change.incident_id is not None:
            self.monitor.set_status(change.incident_id, "resolved", note=f"repaired by change request {change.id} ({change.candidate_version})")
        return change

    def rollback(self, integration: str, actor: str, note: str = "") -> VersionRecord:
        current = self.registry.get_published(integration)
        restored = self.registry.rollback(integration)
        with self.db.session() as s:
            row = s.scalar(
                select(ChangeRequestRow).where(
                    ChangeRequestRow.integration == integration,
                    ChangeRequestRow.candidate_version == current.version,
                    ChangeRequestRow.status == "promoted",
                )
            )
            if row is not None:
                row.status = "rolled_back"
                row.decision_note = (row.decision_note + "; " if row.decision_note else "") + f"rolled back by {actor}: {note}".rstrip(": ")
                incident_id = row.incident_id
                s.commit()
                if incident_id is not None:
                    self.monitor.set_status(incident_id, "needs_human", note=f"change request {row.id} was rolled back by {actor}")
        return restored

    # --- reads ------------------------------------------------------------------------------------

    def get(self, change_id: int) -> ChangeRequest:
        with self.db.session() as s:
            return ChangeRequest.from_row(self._row(s, change_id))

    def list(self, integration: str | None = None, status: ChangeStatus | None = None) -> list[ChangeRequest]:
        with self.db.session() as s:
            query = select(ChangeRequestRow).order_by(ChangeRequestRow.id)
            if integration:
                query = query.where(ChangeRequestRow.integration == integration)
            if status:
                query = query.where(ChangeRequestRow.status == status)
            return [ChangeRequest.from_row(r) for r in s.scalars(query)]

    @staticmethod
    def _row(s: Any, change_id: int) -> ChangeRequestRow:
        row = s.get(ChangeRequestRow, change_id)
        if row is None:
            raise NotFound(f"change request {change_id} not found")
        return row
