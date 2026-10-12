"""Drift incidents, change requests, approval policies and canary observations.

An incident aggregates DriftEvents of one kind on one endpoint of one integration while it is
unresolved. A change request carries a candidate manifest version through approval, canary and
promotion. Approval policies decide, per integration and risk class, whether a change request
may skip the human. Every decision is recorded with its actor and time.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, as_utc, utcnow

IncidentStatus = Literal["open", "triaged", "in_repair", "needs_human", "repair_failed", "resolved", "dismissed"]
DriftClass = Literal["schema", "behavioral", "auth", "deprecation", "semantic", "transient", "cosmetic"]
RiskClass = Literal["low", "medium", "high"]
ChangeStatus = Literal["pending", "approved", "rejected", "canary", "promoted", "aborted", "failed", "rolled_back"]
CanaryArm = Literal["base", "candidate"]


class DriftIncidentRow(Base):
    __tablename__ = "drift_incidents"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration: Mapped[str] = mapped_column(String(64), index=True)
    endpoint_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    version: Mapped[str] = mapped_column(String(32))
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    count: Mapped[int] = mapped_column(Integer, default=0)
    samples_json: Mapped[str] = mapped_column(Text, default="[]")
    sample_body_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    drift_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    risk_class: Mapped[str | None] = mapped_column(String(10), nullable=True)
    repairable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    triage_note: Mapped[str] = mapped_column(Text, default="")
    change_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChangeRequestRow(Base):
    __tablename__ = "change_requests"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration: Mapped[str] = mapped_column(String(64), index=True)
    incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_version: Mapped[str] = mapped_column(String(32))
    candidate_version: Mapped[str] = mapped_column(String(32))
    drift_class: Mapped[str] = mapped_column(String(20))
    risk_class: Mapped[str] = mapped_column(String(10))
    strategy: Mapped[str] = mapped_column(String(120))
    diff_json: Mapped[str] = mapped_column(Text, default="[]")
    verification_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    auto_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    canary_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    canary_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalPolicyRow(Base):
    """Default is no row, which means every change request waits for a human."""

    __tablename__ = "approval_policies"
    integration: Mapped[str] = mapped_column(String(64), primary_key=True)
    risk_class: Mapped[str] = mapped_column(String(10), primary_key=True)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CanaryObservationRow(Base):
    __tablename__ = "canary_observations"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    change_request_id: Mapped[int] = mapped_column(Integer, index=True)
    arm: Mapped[str] = mapped_column(String(10))
    version: Mapped[str] = mapped_column(String(32))
    endpoint_id: Mapped[str] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Boolean)
    validation_errors: Mapped[int] = mapped_column(Integer, default=0)
    drift_events: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- DTOs ----------------------------------------------------------------------------------


class Triage(BaseModel):
    drift_class: DriftClass
    risk_class: RiskClass
    repairable: bool
    rationale: str


class DriftIncident(BaseModel):
    id: int
    integration: str
    endpoint_id: str
    kind: str
    status: IncidentStatus
    version: str
    tenant_id: str | None = None
    status_code: int | None = None
    first_seen: datetime
    last_seen: datetime
    count: int
    samples: list[str] = Field(default_factory=list)
    sample_body: Any = None
    drift_class: DriftClass | None = None
    risk_class: RiskClass | None = None
    repairable: bool | None = None
    triage_note: str = ""
    change_request_id: int | None = None
    note: str = ""
    resolved_at: datetime | None = None

    @classmethod
    def from_row(cls, row: DriftIncidentRow) -> "DriftIncident":
        return cls(
            id=row.id,
            integration=row.integration,
            endpoint_id=row.endpoint_id,
            kind=row.kind,
            status=row.status,  # type: ignore[arg-type]
            version=row.version,
            tenant_id=row.tenant_id,
            status_code=row.status_code,
            first_seen=as_utc(row.first_seen) or utcnow(),
            last_seen=as_utc(row.last_seen) or utcnow(),
            count=row.count,
            samples=json.loads(row.samples_json or "[]"),
            sample_body=json.loads(row.sample_body_json) if row.sample_body_json else None,
            drift_class=row.drift_class,  # type: ignore[arg-type]
            risk_class=row.risk_class,  # type: ignore[arg-type]
            repairable=row.repairable,
            triage_note=row.triage_note or "",
            change_request_id=row.change_request_id,
            note=row.note or "",
            resolved_at=as_utc(row.resolved_at),
        )


class ChangeRequest(BaseModel):
    id: int
    integration: str
    incident_id: int | None
    base_version: str
    candidate_version: str
    drift_class: DriftClass
    risk_class: RiskClass
    strategy: str
    diff: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    verified: bool
    status: ChangeStatus
    auto_approved: bool
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str = ""
    canary_fraction: float | None = None
    canary_started_at: datetime | None = None
    canary: dict[str, Any] | None = None
    promoted_at: datetime | None = None

    @classmethod
    def from_row(cls, row: ChangeRequestRow) -> "ChangeRequest":
        return cls(
            id=row.id,
            integration=row.integration,
            incident_id=row.incident_id,
            base_version=row.base_version,
            candidate_version=row.candidate_version,
            drift_class=row.drift_class,  # type: ignore[arg-type]
            risk_class=row.risk_class,  # type: ignore[arg-type]
            strategy=row.strategy,
            diff=json.loads(row.diff_json or "[]"),
            verification=json.loads(row.verification_json) if row.verification_json else None,
            verified=row.verified,
            status=row.status,  # type: ignore[arg-type]
            auto_approved=row.auto_approved,
            created_at=as_utc(row.created_at) or utcnow(),
            decided_by=row.decided_by,
            decided_at=as_utc(row.decided_at),
            decision_note=row.decision_note or "",
            canary_fraction=row.canary_fraction,
            canary_started_at=as_utc(row.canary_started_at),
            canary=json.loads(row.canary_json) if row.canary_json else None,
            promoted_at=as_utc(row.promoted_at),
        )


class ApprovalPolicy(BaseModel):
    integration: str
    auto_approve: dict[RiskClass, bool] = Field(default_factory=lambda: {"low": False, "medium": False, "high": False})


class ArmStats(BaseModel):
    version: str
    calls: int = 0
    failures: int = 0
    validation_errors: int = 0
    drift_events: int = 0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls else 0.0


class CanaryReport(BaseModel):
    change_request_id: int
    fraction: float
    min_calls: int
    base: ArmStats
    candidate: ArmStats
    verdict: Literal["insufficient", "pass", "fail"]
    reason: str
