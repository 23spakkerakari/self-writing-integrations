"""Shared SQLAlchemy base and engine. The registry, tenancy, vault and audit tables all live in
one database so a single Database object serves every store."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.url = url
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        # Import every module that declares tables so metadata is complete before create_all.
        import app.drift.models  # noqa: F401
        import app.oauth.models  # noqa: F401
        import app.registry.store  # noqa: F401

        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self._sessions()


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; normalize anything we read back to aware UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
