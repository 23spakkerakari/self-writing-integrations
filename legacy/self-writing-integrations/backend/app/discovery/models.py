"""Traffic samples, inferred endpoints and mapping proposals.

A traffic sample is one request/response pair, stored after redaction. An inferred endpoint is
computed from samples on demand and never stored, so it always reflects every sample captured so
far. A mapping proposal says "this raw field is this canonical field"; it stays `proposed` until a
person confirms or rejects it, and that decision survives when proposals are generated again.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, as_utc, utcnow

SampleSource = Literal["har", "probe", "proxy", "manual"]
ProposalStatus = Literal["proposed", "confirmed", "rejected"]
Direction = Literal["read", "write"]


class TrafficSampleRow(Base):
    __tablename__ = "traffic_samples"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)
    query_json: Mapped[str] = mapped_column(Text, default="{}")
    request_headers_json: Mapped[str] = mapped_column(Text, default="{}")
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[int] = mapped_column(Integer)
    response_headers_json: Mapped[str] = mapped_column(Text, default="{}")
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_hint: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MappingProposalRow(Base):
    __tablename__ = "mapping_proposals"
    __table_args__ = (UniqueConstraint("integration", "endpoint_key", "direction", "canonical_object", "target"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration: Mapped[str] = mapped_column(String(64), index=True)
    endpoint_key: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(8), default="read")
    canonical_object: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(64))
    source_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    alternatives_json: Mapped[str] = mapped_column(Text, default="[]")
    proposer: Mapped[str] = mapped_column(String(64), default="heuristic")
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- DTOs ----------------------------------------------------------------------------------


class TrafficSample(BaseModel):
    """One stored exchange. Secrets were replaced before it was written."""

    id: int
    integration: str
    method: str
    url: str
    host: str
    path: str
    query: dict[str, str] = Field(default_factory=dict)
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str | None = None
    status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: str | None = None
    auth_hint: str = ""
    source: SampleSource = "manual"
    captured_at: datetime

    @classmethod
    def from_row(cls, row: TrafficSampleRow) -> "TrafficSample":
        return cls(
            id=row.id,
            integration=row.integration,
            method=row.method,
            url=row.url,
            host=row.host,
            path=row.path,
            query=json.loads(row.query_json or "{}"),
            request_headers=json.loads(row.request_headers_json or "{}"),
            request_body=row.request_body,
            status=row.status,
            response_headers=json.loads(row.response_headers_json or "{}"),
            response_body=row.response_body,
            auth_hint=row.auth_hint or "",
            source=row.source,  # type: ignore[arg-type]
            captured_at=as_utc(row.captured_at) or utcnow(),
        )

    def response_json(self) -> Any:
        return _parse(self.response_body)

    def request_json(self) -> Any:
        return _parse(self.request_body)


NOT_JSON = object()


def _parse(text: str | None) -> Any:
    if not text:
        return NOT_JSON
    try:
        return json.loads(text)
    except ValueError:
        return NOT_JSON


class SampleSummary(BaseModel):
    """What the API returns for a sample: where it went and how it ended, not the bodies."""

    id: int
    method: str
    url: str
    status: int
    source: SampleSource
    captured_at: datetime
    response_bytes: int


class SystemSummary(BaseModel):
    integration: str
    samples: int
    hosts: list[str]
    first_captured: datetime | None = None
    last_captured: datetime | None = None
    proposals: int = 0
    confirmed: int = 0
    undecided: int = 0


class FieldStat(BaseModel):
    """What the samples say about one field of a record."""

    path: str = Field(description="Dotted path inside one record; a list of objects is written 'jobs[].title'")
    types: list[str] = Field(description="JSON types seen, null excluded")
    nullable: bool = False
    present: int = Field(description="Records that carried the field")
    total: int = Field(description="Records examined")
    distinct: int = 0
    format: str | None = None
    enum: list[Any] | None = Field(default=None, description="Values, when the field repeats a small closed set")
    examples: list[Any] = Field(default_factory=list)
    confidence: float = Field(description="How far the samples support this description of the field, 0 to 1")


class QueryParam(BaseModel):
    name: str
    values: list[str] = Field(default_factory=list)
    constant: bool = False
    role: Literal["parameter", "default", "pagination", "auth"] = "parameter"


class InferredEndpoint(BaseModel):
    key: str = Field(description="Method and path template, e.g. 'GET /employees/{employee_id}'")
    method: str
    path: str
    path_params: list[str] = Field(default_factory=list)
    query_params: list[QueryParam] = Field(default_factory=list)
    samples: int = 0
    statuses: dict[str, int] = Field(default_factory=dict)
    is_list: bool = False
    items_path: str | None = None
    records: int = 0
    response_schema: dict[str, Any] | None = None
    fields: list[FieldStat] = Field(default_factory=list)
    pagination: dict[str, Any] | None = None
    request_schema: dict[str, Any] | None = None
    request_fields: list[FieldStat] = Field(default_factory=list)
    canonical_object: str | None = Field(default=None, description="Canonical object the records look like, if any")
    confidence: float = 0.0
    notes: list[str] = Field(default_factory=list)


class DiscoveryReport(BaseModel):
    integration: str
    samples: int
    hosts: list[str] = Field(default_factory=list)
    base_url: str = ""
    auth: dict[str, Any] = Field(default_factory=lambda: {"type": "none"})
    default_headers: dict[str, str] = Field(default_factory=dict)
    endpoints: list[InferredEndpoint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Alternative(BaseModel):
    source_path: str | None
    confidence: float
    transform: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    proposer: str = "heuristic"


class Suggestion(BaseModel):
    """A mapping guess before it is stored."""

    canonical_object: str
    target: str
    source_path: str | None = None
    transform: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    rationale: str
    alternatives: list[Alternative] = Field(default_factory=list)
    proposer: str = "heuristic"


class MappingProposal(BaseModel):
    id: int
    integration: str
    endpoint_key: str
    direction: Direction
    canonical_object: str
    target: str = Field(description="Canonical field")
    source_path: str | None = Field(description="Raw field: read from it on a read endpoint, written to it on a write endpoint")
    transform: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    rationale: str
    alternatives: list[Alternative] = Field(default_factory=list)
    proposer: str
    status: ProposalStatus
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str = ""
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: MappingProposalRow) -> "MappingProposal":
        return cls(
            id=row.id,
            integration=row.integration,
            endpoint_key=row.endpoint_key,
            direction=row.direction,  # type: ignore[arg-type]
            canonical_object=row.canonical_object,
            target=row.target,
            source_path=row.source_path,
            transform=row.transform,
            args=json.loads(row.args_json or "{}"),
            confidence=row.confidence,
            rationale=row.rationale or "",
            alternatives=json.loads(row.alternatives_json or "[]"),
            proposer=row.proposer,
            status=row.status,  # type: ignore[arg-type]
            decided_by=row.decided_by,
            decided_at=as_utc(row.decided_at),
            note=row.note or "",
            created_at=as_utc(row.created_at) or utcnow(),
            updated_at=as_utc(row.updated_at) or utcnow(),
        )


class ConceptMember(BaseModel):
    integration: str
    endpoint_key: str
    direction: Direction
    source_path: str | None
    status: ProposalStatus
    confidence: float


class Concept(BaseModel):
    """One canonical field and every raw field, across systems, believed to carry it."""

    canonical_object: str
    target: str
    members: list[ConceptMember]
