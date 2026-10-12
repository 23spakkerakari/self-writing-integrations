"""systems, sources, source_health and the ingest batch ledger (spec 7.3, ADR 0015)

Revision ID: 0001
Revises:
Create Date: 2026-10-08 00:00:00 UTC

Every table carries ``tenant_id``, ``created_at`` and ``updated_at`` (spec 7.3, 5.5 "Keep
tenant_id on every table from day one"). ``sources.config_json`` holds connector configuration
without secrets; the secret lives behind ``secret_ref`` (spec 14.3). ``source_health`` is what
``POST /internal/heartbeat`` upserts (spec 12); it has no foreign key to ``sources`` because in
M1 sources are pinned in the edge sources file and a heartbeat may arrive before core knows the
source (M2 registers them). ``ingest_batches`` is the idempotency ledger of ``ingest-api`` and
the bundle loader: a ``(tenant_id, batch_id)`` already present is acknowledged, not rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "systems",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_group", sa.String(256), nullable=True),
        sa.Column("criticality", sa.String(16), nullable=False, server_default="medium"),
        *_audit_columns(),
    )
    op.create_index("ix_systems_tenant_id", "systems", ["tenant_id"])

    op.create_table(
        "sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "system_id",
            sa.String(64),
            sa.ForeignKey("systems.id", name="fk_sources_system_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column(
            "config_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Connector configuration without secrets (spec 14.3).",
        ),
        sa.Column("secret_ref", sa.String(1024), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("cursor_json", postgresql.JSONB(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lag_seconds", sa.Float(), nullable=True),
        *_audit_columns(),
    )
    op.create_index("ix_sources_tenant_id", "sources", ["tenant_id"])
    op.create_index("ix_sources_system_id", "sources", ["system_id"])

    op.create_table(
        "source_health",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lag_seconds", sa.Float(), nullable=False),
        sa.Column("error_count", sa.BigInteger(), nullable=False),
        sa.Column("buffer_depth", sa.BigInteger(), nullable=False),
        sa.Column("oldest_buffered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "source_id", name="pk_source_health"),
    )

    op.create_table(
        "ingest_batches",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(26), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "batch_id", name="pk_ingest_batches"),
    )
    op.create_index("ix_ingest_batches_received_at", "ingest_batches", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_ingest_batches_received_at", table_name="ingest_batches")
    op.drop_table("ingest_batches")
    op.drop_table("source_health")
    op.drop_index("ix_sources_system_id", table_name="sources")
    op.drop_index("ix_sources_tenant_id", table_name="sources")
    op.drop_table("sources")
    op.drop_index("ix_systems_tenant_id", table_name="systems")
    op.drop_table("systems")
