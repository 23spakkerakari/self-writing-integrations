"""Alembic environment for the carto metadata store (spec 7.3).

The supported path is programmatic: ``carto_core.migrations.apply_postgres_migrations`` puts an
open SQLAlchemy connection in ``config.attributes["connection"]`` and calls ``upgrade``. The
``alembic`` command line works only with ``-x url=<sqlalchemy url>`` so that no credential ever
sits in ``alembic.ini`` (spec 14.3). Alembic's own logging configuration is not loaded: the
service's structlog setup (``carto_common.logging``) owns the root logger.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

config = context.config
target_metadata = None


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = context.get_x_argument(as_dictionary=True).get("url")
    if not url:
        msg = (
            "no connection: run `carto-core migrate` (settings from the environment) or pass "
            "-x url=postgresql+psycopg://..."
        )
        raise RuntimeError(msg)
    engine = create_engine(url)
    try:
        with engine.begin() as owned:
            _run(owned)
    finally:
        engine.dispose()


if context.is_offline_mode():
    msg = "offline (SQL script) mode is not supported; migrations run against a live database"
    raise RuntimeError(msg)
run_migrations_online()
