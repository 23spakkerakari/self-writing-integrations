"""SQL polling connector for PostgreSQL, MySQL and SQL Server (spec 8.1.4).

Admin-written query templates (Appendix A) are checked by ``sqlglot`` at construction
(:func:`carto_edge.connectors.sql_dialects.check_statement`: one SELECT, ``:watermark`` and
``:batch`` only), run through SQLAlchemy 2 core ``text()`` on synchronous engines driven with
``asyncio.to_thread``, inside read-only sessions (PostgreSQL ``default_transaction_read_only``
plus ``BEGIN READ ONLY``; MySQL ``START TRANSACTION READ ONLY``; SQL Server
``ApplicationIntent=ReadOnly``). ``test()`` runs every query with ``batch=1`` and then inspects
the login's privileges on every referenced table; a login that can INSERT, UPDATE, DELETE or
alter anything makes the result ``WRITE_CAPABLE`` and the source cannot be enabled.

Each poll opens a fresh DBAPI connection (``NullPool``) through a ``creator``, so credentials are
resolved at use (spec 14.3) and the host is validated by the network policy at every connect
(spec 14.7): PostgreSQL pins the address with ``hostaddr``; MySQL and SQL Server connect by
name after validation (ADR 0018).

Records: ``fields`` is the row as Python values (the parser stringifies), locator
``<query name>:row:<primary key>`` (ADR 0006), kind ``row_change``, template hint
``row_change <query name>`` (ADR 0016); the last record of a page carries
``commit_cursor={"watermarks": {name: value}}``. Rows that share the largest watermark value of
a full page can be re-read on the next poll (at-least-once; core dedupes by ``event_id``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from carto_edge.config import SourceConfig
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorError,
    Cursor,
    InvalidConfigError,
    ReadOnlyStatus,
    TestCheck,
    TestResult,
)
from carto_edge.connectors.registry import register, validate_config_model
from carto_edge.connectors.sql_dialects import (
    CheckedStatement,
    Dialect,
    check_statement,
    mssql_connection_string,
    mysql_connect_kwargs,
    mysql_session_statements,
    postgres_connect_kwargs,
    privilege_queries,
    write_findings,
)
from carto_edge.net.retry import CircuitBreaker
from carto_edge.net.ssrf import SsrfError
from carto_edge.pipeline.model import RawRecord
from carto_edge.secrets import Credentials, SecretError, parse_credentials
from carto_schema.event import EventKind

__all__ = ["ConnectionPlan", "SqlConfig", "SqlConnector", "SqlQueryConfig"]

logger = logging.getLogger(__name__)

DEFAULT_PORTS: Final[dict[str, int]] = {"postgresql": 5432, "mysql": 3306, "mssql": 1433}
DRIVERS: Final[dict[str, str]] = {
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mssql": "mssql+pyodbc",
}
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ISO_DATETIME: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"
)
_ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ERROR_CHARS: Final = 200

SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
ConnectionPlan = dict[str, Any] | str
"""psycopg / PyMySQL keyword arguments, or the pyodbc connection string."""
DbapiConnect = Callable[[str, ConnectionPlan], Any]
"""``(dialect, plan) -> DBAPI connection``; injectable for the SQL Server fake."""
EngineFactory = Callable[[str, Callable[[], Any]], Engine]
"""``(drivername, creator) -> Engine``; injectable for tests."""


class SqlQueryConfig(ConnectorConfig):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    sql: str = Field(min_length=1, max_length=16_384)
    watermark_column: str = Field(min_length=1, max_length=128)
    timestamp_column: str | None = Field(default=None, max_length=128)
    actor_column: str | None = Field(default=None, max_length=128)
    primary_key: str | None = Field(default=None, max_length=128)


class SqlConfig(ConnectorConfig):
    dialect: Dialect
    host: str = Field(min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    sslmode: SslMode = Field(default="verify-full", description="PostgreSQL libpq sslmode.")
    ssl: bool = Field(default=True, description="MySQL: TLS with certificate verification.")
    encrypt: bool = Field(default=True, description="SQL Server: Encrypt=yes.")
    trust_server_certificate: bool = False
    driver: str = Field(default="ODBC Driver 18 for SQL Server", min_length=1, max_length=128)
    ca_file: str | None = Field(default=None, max_length=4096)
    poll_seconds: int = Field(default=60, ge=5, le=86_400)
    query_timeout_seconds: int = Field(default=30, ge=1, le=3600)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    batch: int = Field(default=1000, ge=1, le=100_000)
    max_pages_per_read: int = Field(default=100, ge=1, le=10_000)
    failure_threshold: int = Field(default=5, ge=1, le=100)
    breaker_reset_seconds: float = Field(default=60.0, gt=0, le=3600)
    queries: list[SqlQueryConfig] = Field(min_length=1, max_length=64)

    @field_validator("host")
    @classmethod
    def _plain_host(cls, value: str) -> str:
        if "@" in value or "/" in value or any(char.isspace() for char in value):
            msg = "host must be a bare host name or address"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _unique_query_names(self) -> SqlConfig:
        names = [query.name for query in self.queries]
        if len(set(names)) != len(names):
            msg = "query names must be unique"
            raise ValueError(msg)
        return self


def _encode_watermark(value: object) -> object:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
        return moment.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _decode_watermark(value: object) -> object:
    if isinstance(value, str):
        if _ISO_DATETIME.match(value):
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                return value
            return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment
        if _ISO_DATE.match(value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return value
    return value


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


def _max_watermark(rows: list[dict[str, Any]], column: str, current: object) -> object:
    best = current
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        if isinstance(value, datetime):
            value = _naive_utc(value)
        try:
            if best is None or value > best:  # type: ignore[operator]
                best = value
        except TypeError:
            continue
    return best


def _dbapi_connect(dialect: str, plan: ConnectionPlan) -> Any:
    if dialect == "postgresql":
        import psycopg  # noqa: PLC0415 - driver imports stay lazy

        assert isinstance(plan, dict)  # noqa: S101
        return psycopg.connect(**plan)
    if dialect == "mysql":
        import pymysql  # noqa: PLC0415

        assert isinstance(plan, dict)  # noqa: S101
        return pymysql.connect(**plan)
    import pyodbc  # noqa: PLC0415

    assert isinstance(plan, str)  # noqa: S101
    return pyodbc.connect(plan)


def _default_engine(drivername: str, creator: Callable[[], Any]) -> Engine:
    return create_engine(f"{drivername}://", creator=creator, poolclass=NullPool)


def _error_text(exc: BaseException) -> str:
    """A short, credential-free description of a database error."""
    origin = getattr(exc, "orig", None)
    source = origin if isinstance(origin, BaseException) else exc
    first_line = str(source).splitlines()[0] if str(source) else ""
    return f"{type(source).__name__}: {first_line[:MAX_ERROR_CHARS]}"


@register("sql")
class SqlConnector:
    """Spec 8.1.4."""

    type: ClassVar[str] = "sql"

    def __init__(
        self,
        source: SourceConfig,
        context: ConnectorContext,
        *,
        connect: DbapiConnect | None = None,
        engine_factory: EngineFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.context = context
        self.config = self.validate_config(source.config)
        if source.secret_ref is None:
            msg = f"source {source.id!r}: a sql source needs a secret_ref for its login"
            raise InvalidConfigError(msg)
        self.statements: dict[str, CheckedStatement] = {}
        for query in self.config.queries:
            try:
                self.statements[query.name] = check_statement(query.sql, self.config.dialect)
            except InvalidConfigError as exc:
                msg = f"source {source.id!r} query {query.name!r}: {exc}"
                raise InvalidConfigError(msg) from exc
        self._connect = connect if connect is not None else _dbapi_connect
        self._engine_factory = engine_factory if engine_factory is not None else _default_engine
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._engine: Engine | None = None
        self._breaker = CircuitBreaker(
            self.config.failure_threshold, self.config.breaker_reset_seconds
        )
        self._sequence = 0

    def validate_config(self, cfg: Mapping[str, Any]) -> SqlConfig:
        return validate_config_model(SqlConfig, cfg, self.source.id)

    @property
    def port(self) -> int:
        return self.config.port or DEFAULT_PORTS[self.config.dialect]

    @property
    def tables(self) -> tuple[str, ...]:
        names: list[str] = []
        for statement in self.statements.values():
            names.extend(table for table in statement.tables if table not in names)
        return tuple(names)

    # -- connections ----------------------------------------------------------------------------

    def _credentials(self) -> Credentials:
        try:
            return parse_credentials(self.context.secrets.resolve(self.source.secret_ref or ""))
        except SecretError as exc:
            msg = f"source {self.source.id!r}: login secret is unusable: {exc}"
            raise ConnectorError(msg) from exc

    def connection_plan(self) -> ConnectionPlan:
        """Validate the host through the network policy, resolve the login and build the driver
        arguments. Called on every new DBAPI connection."""
        cfg = self.config
        try:
            resolved = self.context.network.resolve(cfg.host, self.port)
        except SsrfError as exc:
            msg = f"source {self.source.id!r}: {exc}"
            raise ConnectorError(msg) from exc
        credentials = self._credentials()
        if cfg.dialect == "postgresql":
            return postgres_connect_kwargs(
                host=cfg.host,
                resolved=resolved,
                port=self.port,
                database=cfg.database,
                credentials=credentials,
                sslmode=cfg.sslmode,
                ca_file=cfg.ca_file,
                connect_timeout_seconds=cfg.connect_timeout_seconds,
                query_timeout_seconds=cfg.query_timeout_seconds,
            )
        if cfg.dialect == "mysql":
            return mysql_connect_kwargs(
                host=cfg.host,
                port=self.port,
                database=cfg.database,
                credentials=credentials,
                ssl=cfg.ssl,
                ca_file=cfg.ca_file,
                connect_timeout_seconds=cfg.connect_timeout_seconds,
                query_timeout_seconds=cfg.query_timeout_seconds,
            )
        return mssql_connection_string(
            host=cfg.host,
            port=self.port,
            database=cfg.database,
            credentials=credentials,
            driver=cfg.driver,
            encrypt=cfg.encrypt,
            trust_server_certificate=cfg.trust_server_certificate,
            connect_timeout_seconds=cfg.connect_timeout_seconds,
        )

    def _creator(self) -> Any:
        connection = self._connect(self.config.dialect, self.connection_plan())
        if self.config.dialect == "mssql":
            connection.timeout = self.config.query_timeout_seconds  # pyodbc query timeout
        return connection

    def _engine_or_create(self) -> Engine:
        if self._engine is None:
            self._engine = self._engine_factory(DRIVERS[self.config.dialect], self._creator)
        return self._engine

    @contextmanager
    def _read_only_session(self) -> Iterator[Connection]:
        """A connection inside a read-only transaction (spec 8.1.4 layer 2)."""
        engine = self._engine_or_create()
        dialect = self.config.dialect
        with engine.connect() as connection:
            if dialect == "postgresql":
                # psycopg emits BEGIN READ ONLY for this option; default_transaction_read_only
                # in the connection options backs it up.
                session = connection.execution_options(postgresql_readonly=True)
                try:
                    yield session
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
            elif dialect == "mysql":
                session = connection.execution_options(isolation_level="AUTOCOMMIT")
                for statement in mysql_session_statements(self.config.query_timeout_seconds):
                    session.exec_driver_sql(statement)
                session.exec_driver_sql("SET SESSION time_zone = '+00:00'")
                session.exec_driver_sql("START TRANSACTION READ ONLY")
                try:
                    yield session
                    session.exec_driver_sql("COMMIT")
                except BaseException:
                    session.exec_driver_sql("ROLLBACK")
                    raise
            else:
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def _run(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            with self._read_only_session() as connection:
                result = connection.execute(text(sql), dict(params))
                return [dict(row) for row in result.mappings().all()]
        except SQLAlchemyError as exc:
            msg = f"source {self.source.id!r}: query failed: {_error_text(exc)}"
            raise ConnectorError(msg) from exc

    async def _run_async(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return await self._breaker.call(lambda: asyncio.to_thread(self._run, sql, params))

    # -- records --------------------------------------------------------------------------------

    def _initial_watermark(self) -> object:
        if self.source.backfill_days:
            return _naive_utc(self._clock() - timedelta(days=self.source.backfill_days))
        return _naive_utc(EPOCH)

    def _record(self, query: SqlQueryConfig, row: dict[str, Any], now: datetime) -> RawRecord:
        key_column = query.primary_key or next(iter(row), "")
        key_value = row.get(key_column)
        self._sequence += 1
        locator = (
            f"{query.name}:row:{key_value}"
            if key_value is not None
            else f"{query.name}:row:seq:{self._sequence}"
        )
        return RawRecord(
            source_id=self.source.id,
            system_id=self.source.system,
            kind=EventKind.ROW_CHANGE,
            locator=locator,
            received_at=now,
            fields=row,
            sequence=self._sequence,
            size_bytes=sum(len(str(value)) for value in row.values() if value is not None),
            template_hint=f"row_change {query.name}",
        )

    @staticmethod
    def _with_cursor(record: RawRecord, watermarks: Mapping[str, object]) -> RawRecord:
        return RawRecord(
            source_id=record.source_id,
            system_id=record.system_id,
            kind=record.kind,
            locator=record.locator,
            received_at=record.received_at,
            fields=record.fields,
            sequence=record.sequence,
            commit_cursor={
                "watermarks": {name: _encode_watermark(value) for name, value in watermarks.items()}
            },
            size_bytes=record.size_bytes,
            template_hint=record.template_hint,
        )

    async def read(self, cursor: Cursor | None) -> AsyncIterator[RawRecord]:
        now = self._clock()
        raw = cursor.get("watermarks") if cursor else None
        watermarks: dict[str, object] = (
            {str(name): _decode_watermark(value) for name, value in raw.items()}
            if isinstance(raw, Mapping)
            else {}
        )
        total = 0
        started = time.monotonic()
        for query in self.config.queries:
            watermark = watermarks.get(query.name, self._initial_watermark())
            for _page in range(self.config.max_pages_per_read):
                rows = await self._run_async(
                    self.statements[query.name].sql,
                    {"watermark": watermark, "batch": self.config.batch},
                )
                if not rows:
                    break
                advanced = _max_watermark(rows, query.watermark_column, watermark)
                progressed = advanced != watermark
                if progressed:
                    watermarks[query.name] = advanced
                for index, row in enumerate(rows):
                    record = self._record(query, row, now)
                    total += 1
                    yield (
                        self._with_cursor(record, watermarks) if index == len(rows) - 1 else record
                    )
                if not progressed:
                    logger.warning(
                        "sql watermark did not advance source_id=%s query=%s rows=%d",
                        self.source.id,
                        query.name,
                        len(rows),
                    )
                    break
                watermark = advanced
                if len(rows) < self.config.batch:
                    break
        logger.info(
            "sql poll source_id=%s queries=%d records=%d seconds=%.1f",
            self.source.id,
            len(self.config.queries),
            total,
            time.monotonic() - started,
        )

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        now = self._clock()
        lower, upper = _naive_utc(start), _naive_utc(end)
        for query in self.config.queries:
            watermark: object = lower
            for _page in range(self.config.max_pages_per_read):
                rows = await self._run_async(
                    self.statements[query.name].sql,
                    {"watermark": watermark, "batch": self.config.batch},
                )
                if not rows:
                    break
                for row in rows:
                    value = row.get(query.watermark_column)
                    if isinstance(value, datetime) and _naive_utc(value) > upper:
                        continue
                    yield self._record(query, row, now)
                advanced = _max_watermark(rows, query.watermark_column, watermark)
                if advanced == watermark or (isinstance(advanced, datetime) and advanced >= upper):
                    break
                watermark = advanced
                if len(rows) < self.config.batch:
                    break

    async def close(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            await asyncio.to_thread(engine.dispose)

    # -- test() ---------------------------------------------------------------------------------

    async def test(self) -> TestResult:
        checks: list[TestCheck] = []
        problems: list[str] = []
        for query in self.config.queries:
            try:
                rows = await self._run_async(
                    self.statements[query.name].sql, {"watermark": _naive_utc(EPOCH), "batch": 1}
                )
            except ConnectorError as exc:
                checks.append(TestCheck(f"query {query.name}", False, str(exc)))
                problems.append(f"query {query.name!r} failed: {exc}")
                continue
            columns = ", ".join(rows[0].keys()) if rows else "no rows yet"
            checks.append(TestCheck(f"query {query.name}", True, columns))
        findings: list[str] = []
        for privilege_query in privilege_queries(self.config.dialect, self.tables):
            try:
                rows = await self._run_async(privilege_query.sql, privilege_query.params)
            except ConnectorError as exc:
                checks.append(TestCheck(f"privileges {privilege_query.name}", False, str(exc)))
                problems.append(f"privilege check {privilege_query.name!r} failed: {exc}")
                continue
            found = write_findings(self.config.dialect, privilege_query, rows)
            findings.extend(found)
            label = privilege_query.table or privilege_query.name
            checks.append(
                TestCheck(
                    f"privileges {label}", not found, "; ".join(found) if found else "read-only"
                )
            )
        if findings:
            problems.append(
                "the login can write, so the source cannot be enabled (spec 8.1.4): "
                + "; ".join(findings)
            )
        read_only = ReadOnlyStatus.WRITE_CAPABLE if findings else ReadOnlyStatus.VERIFIED
        return TestResult(
            ok=not problems,
            read_only=read_only,
            checks=tuple(checks),
            problems=tuple(problems),
            visible=self.tables,
        )
