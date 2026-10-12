"""Tenancy, connections, vault storage, audit and notifications.

A Tenant owns Connections. A Connection binds a tenant to an integration with config values and
a credential set in the vault. Every credential event is appended to auth_events so the full
history of a connection can be reconstructed for compliance.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, as_utc, utcnow

ConnectionStatus = Literal["pending_consent", "active", "needs_reconsent", "revoked"]


def new_id() -> str:
    return uuid.uuid4().hex


class TenantRow(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantKeyRow(Base):
    """Per-tenant data encryption key, wrapped with the vault master key."""

    __tablename__ = "tenant_keys"
    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthAppRow(Base):
    """The platform's registered OAuth client for one integration (one per provider, all tenants)."""

    __tablename__ = "oauth_apps"
    integration_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(200))
    client_secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    redirect_uri: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectionRow(Base):
    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    integration_name: Mapped[str] = mapped_column(String(64), index=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending_consent")
    granted_scopes: Mapped[str] = mapped_column(Text, default="")
    pending_state: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    pending_verifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_scopes: Mapped[str] = mapped_column(Text, default="")
    pending_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CredentialRow(Base):
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("connection_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthEventRow(Base):
    """Append-only audit log. Never updated or deleted."""

    __tablename__ = "auth_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    event: Mapped[str] = mapped_column(String(40))
    scopes: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationRow(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- DTOs --------------------------------------------------------------------------------


class Tenant(BaseModel):
    id: str
    name: str
    created_at: datetime


class OAuthApp(BaseModel):
    integration_name: str
    client_id: str
    redirect_uri: str
    created_at: datetime


class ConnectionRecord(BaseModel):
    id: str
    tenant_id: str
    integration_name: str
    config: dict[str, Any] = Field(default_factory=dict)
    status: ConnectionStatus
    granted_scopes: list[str] = Field(default_factory=list)
    token_expires_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    refresh_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: ConnectionRow) -> "ConnectionRecord":
        import json

        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            integration_name=row.integration_name,
            config=json.loads(row.config_json or "{}"),
            status=row.status,  # type: ignore[arg-type]
            granted_scopes=[s for s in row.granted_scopes.split(" ") if s],
            token_expires_at=as_utc(row.token_expires_at),
            last_refreshed_at=as_utc(row.last_refreshed_at),
            refresh_count=row.refresh_count,
            created_at=as_utc(row.created_at) or utcnow(),
            updated_at=as_utc(row.updated_at) or utcnow(),
        )


class AuthEvent(BaseModel):
    id: int
    tenant_id: str
    connection_id: str | None
    event: str
    scopes: list[str] = Field(default_factory=list)
    actor: str
    detail: str
    at: datetime

    @classmethod
    def from_row(cls, row: AuthEventRow) -> "AuthEvent":
        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            event=row.event,
            scopes=[s for s in row.scopes.split(" ") if s],
            actor=row.actor,
            detail=row.detail,
            at=as_utc(row.at) or utcnow(),
        )


class Notification(BaseModel):
    id: int
    tenant_id: str
    connection_id: str | None
    kind: str
    message: str
    read: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: NotificationRow) -> "Notification":
        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            kind=row.kind,
            message=row.message,
            read=row.read,
            created_at=as_utc(row.created_at) or utcnow(),
        )
