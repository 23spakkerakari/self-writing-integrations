"""The PostgreSQL audit recorder and the chain verifier (spec 14.9). Package E."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from carto_core.audit import ChainBreak

__all__ = ["verify_audit_log"]


def verify_audit_log(engine: Engine, tenant_id: str) -> ChainBreak | None:
    del engine, tenant_id
    msg = "package E builds the audit recorder"
    raise NotImplementedError(msg)
