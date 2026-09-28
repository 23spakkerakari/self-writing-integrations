"""Versioned registry of integrations.

Every manifest version is immutable once stored. Status moves draft -> verified -> published;
publishing a new version supersedes the previous published one. Spec snapshots are stored
alongside so the drift monitor (milestone three) can diff what the API looked like at each
synthesis or repair.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base, Database, utcnow
from app.manifest.schema import IntegrationManifest

Status = Literal["draft", "verified", "published", "superseded", "rejected"]


class RegistryError(Exception):
    pass


class NotFound(RegistryError):
    pass


class IntegrationRow(Base):
    __tablename__ = "integrations"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    versions: Mapped[list["VersionRow"]] = relationship(back_populates="integration", cascade="all, delete-orphan")


class VersionRow(Base):
    __tablename__ = "integration_versions"
    __table_args__ = (UniqueConstraint("integration_name", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration_name: Mapped[str] = mapped_column(ForeignKey("integrations.name"))
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    manifest_json: Mapped[str] = mapped_column(Text)
    spec_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(64), default="manual")
    verification_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    integration: Mapped[IntegrationRow] = relationship(back_populates="versions")


class SpecSnapshotRow(Base):
    __tablename__ = "spec_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration_name: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VersionRecord(BaseModel):
    name: str
    version: str
    status: Status
    provenance: str
    manifest: dict[str, Any]
    verification: dict[str, Any] | None = None
    created_at: datetime
    published_at: datetime | None = None

    @classmethod
    def from_row(cls, row: VersionRow) -> "VersionRecord":
        return cls(
            name=row.integration_name,
            version=row.version,
            status=row.status,  # type: ignore[arg-type]
            provenance=row.provenance,
            manifest=json.loads(row.manifest_json),
            verification=json.loads(row.verification_json) if row.verification_json else None,
            created_at=row.created_at,
            published_at=row.published_at,
        )


class IntegrationSummary(BaseModel):
    name: str
    display_name: str
    published_version: str | None
    latest_version: str | None
    version_count: int


class Registry:
    def __init__(self, database: Database | str) -> None:
        self.db = database if isinstance(database, Database) else Database(database)
        self.db.create_all()
        self._sessions = self.db.session

    # --- writes -----------------------------------------------------------------------

    def create_version(
        self, manifest: IntegrationManifest, spec_source: str | None = None, provenance: str = "manual"
    ) -> VersionRecord:
        with self._sessions() as s:
            integration = s.get(IntegrationRow, manifest.name)
            if integration is None:
                integration = IntegrationRow(name=manifest.name, display_name=manifest.display_name)
                s.add(integration)
            existing = s.scalar(
                select(VersionRow).where(VersionRow.integration_name == manifest.name, VersionRow.version == manifest.version)
            )
            if existing is not None:
                raise RegistryError(f"{manifest.name}@{manifest.version} already exists; bump the version")
            row = VersionRow(
                integration_name=manifest.name,
                version=manifest.version,
                manifest_json=manifest.model_dump_json(),
                spec_source=spec_source,
                provenance=provenance,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return VersionRecord.from_row(row)

    def record_verification(self, name: str, version: str, report: dict[str, Any], passed: bool) -> VersionRecord:
        with self._sessions() as s:
            row = self._row(s, name, version)
            row.verification_json = json.dumps(report, default=str)
            if row.status in ("draft", "verified", "rejected"):
                row.status = "verified" if passed else "rejected"
            s.commit()
            s.refresh(row)
            return VersionRecord.from_row(row)

    def publish(self, name: str, version: str) -> VersionRecord:
        with self._sessions() as s:
            row = self._row(s, name, version)
            if row.status != "verified":
                raise RegistryError(f"{name}@{version} is '{row.status}'; only verified versions can be published")
            for other in s.scalars(select(VersionRow).where(VersionRow.integration_name == name, VersionRow.status == "published")):
                other.status = "superseded"
            row.status = "published"
            row.published_at = utcnow()
            s.commit()
            s.refresh(row)
            return VersionRecord.from_row(row)

    def save_snapshot(self, name: str, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._sessions() as s:
            s.add(SpecSnapshotRow(integration_name=name, content=content, content_hash=digest))
            s.commit()
        return digest

    # --- reads ------------------------------------------------------------------------

    def get_version(self, name: str, version: str) -> VersionRecord:
        with self._sessions() as s:
            return VersionRecord.from_row(self._row(s, name, version))

    def get_published(self, name: str) -> VersionRecord:
        with self._sessions() as s:
            row = s.scalar(select(VersionRow).where(VersionRow.integration_name == name, VersionRow.status == "published"))
            if row is None:
                raise NotFound(f"no published version of '{name}'")
            return VersionRecord.from_row(row)

    def list_versions(self, name: str) -> list[VersionRecord]:
        with self._sessions() as s:
            if s.get(IntegrationRow, name) is None:
                raise NotFound(f"unknown integration '{name}'")
            rows = s.scalars(select(VersionRow).where(VersionRow.integration_name == name).order_by(VersionRow.id))
            return [VersionRecord.from_row(r) for r in rows]

    def list_integrations(self) -> list[IntegrationSummary]:
        with self._sessions() as s:
            out: list[IntegrationSummary] = []
            for integration in s.scalars(select(IntegrationRow).order_by(IntegrationRow.name)):
                versions = sorted(integration.versions, key=lambda v: v.id)
                published = next((v.version for v in versions if v.status == "published"), None)
                out.append(
                    IntegrationSummary(
                        name=integration.name,
                        display_name=integration.display_name,
                        published_version=published,
                        latest_version=versions[-1].version if versions else None,
                        version_count=len(versions),
                    )
                )
            return out

    def next_version(self, name: str) -> str:
        """Patch-bump the latest stored version, or 0.1.0 for a new integration."""
        with self._sessions() as s:
            rows = list(s.scalars(select(VersionRow).where(VersionRow.integration_name == name).order_by(VersionRow.id)))
        if not rows:
            return "0.1.0"
        major, minor, patch = (int(x) for x in rows[-1].version.split("."))
        return f"{major}.{minor}.{patch + 1}"

    @staticmethod
    def _row(s: Session, name: str, version: str) -> VersionRow:
        row = s.scalar(select(VersionRow).where(VersionRow.integration_name == name, VersionRow.version == version))
        if row is None:
            raise NotFound(f"{name}@{version} not found")
        return row
