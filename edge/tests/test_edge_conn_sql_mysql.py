"""carto_edge.connectors.sql against MySQL in a container (spec 18.3 read-only tests): SELECT-only
login VERIFIED, INSERT grant WRITE_CAPABLE, polling by watermark, and an INSERT through the
connector's START TRANSACTION READ ONLY session is refused. Skipped when Docker is unreachable."""

from __future__ import annotations

import socket
import warnings
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import ConnectorContext, ReadOnlyStatus, ResolvedHost
from carto_edge.connectors.sql import SqlConnector
from carto_edge.pipeline.model import RawRecord

pytestmark = pytest.mark.integration

QUERY = """
SELECT id, po_num, order_ref, status, warehouse_code, created_by, updated_at
FROM purchase_orders
WHERE updated_at > :watermark
ORDER BY updated_at
LIMIT :batch
"""
RO_PASSWORD = "ro-pw-0123456789"  # noqa: S105
RW_PASSWORD = "rw-pw-0123456789"  # noqa: S105
ROOT_PASSWORD = "root-pw-0123456789"  # noqa: S105


def docker_reachable() -> str | None:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            docker.from_env().ping()
    except Exception as exc:  # any failure means "no docker here"
        return f"Docker daemon unreachable: {type(exc).__name__}"
    return None


DOCKER_SKIP = docker_reachable()


@pytest.fixture(scope="module")
def mysql() -> Iterator[dict[str, Any]]:
    if DOCKER_SKIP:
        pytest.skip(DOCKER_SKIP)
    import pymysql  # noqa: PLC0415
    from testcontainers.community.mysql import MySqlContainer  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        container = MySqlContainer(
            "mysql:8.4",
            username="admin",
            password="admin-pw",  # noqa: S106
            dbname="wms",
            root_password=ROOT_PASSWORD,
        )
        container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(3306))
        root = pymysql.connect(
            host=host,
            port=port,
            user="root",
            password=ROOT_PASSWORD,
            database="wms",
            autocommit=True,
        )
        try:
            with root.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE purchase_orders (id INT AUTO_INCREMENT PRIMARY KEY, "
                    "po_num VARCHAR(32), order_ref VARCHAR(32), status VARCHAR(16), "
                    "warehouse_code VARCHAR(8), created_by VARCHAR(64), "
                    "updated_at DATETIME)"
                )
                base = datetime(2026, 9, 23, 22, 0, 0)
                for index in range(5):
                    cursor.execute(
                        "INSERT INTO purchase_orders "
                        "(po_num, order_ref, status, warehouse_code, created_by, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            f"88-21{index}",
                            f"SO-000447{index}",
                            "SHIPPED",
                            "DC-01",
                            "svc_wms",
                            base.replace(minute=index),
                        ),
                    )
                cursor.execute(f"CREATE USER 'carto_ro'@'%' IDENTIFIED BY '{RO_PASSWORD}'")
                cursor.execute("GRANT SELECT ON wms.purchase_orders TO 'carto_ro'@'%'")
                cursor.execute(f"CREATE USER 'carto_rw'@'%' IDENTIFIED BY '{RW_PASSWORD}'")
                cursor.execute("GRANT SELECT, INSERT ON wms.purchase_orders TO 'carto_rw'@'%'")
        finally:
            root.close()
        yield {"host": host, "port": port}
    finally:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            container.stop()


class Secrets:
    def __init__(self, username: str, password: str) -> None:
        self.value = f"{username}:{password}"

    def resolve(self, secret_ref: str) -> str:
        return self.value


class Network:
    def __init__(self, address: str) -> None:
        self.address = address
        self.calls = 0

    def resolve(self, host: str, port: int) -> ResolvedHost:
        self.calls += 1
        return ResolvedHost(host=host, address=self.address, port=port)


def make(
    mysql: dict[str, Any], username: str, password: str, batch: int = 1000
) -> tuple[SqlConnector, Network]:
    host = mysql["host"]
    address = host if host.replace(".", "").isdigit() else socket.gethostbyname(host)
    network = Network(address)
    source = SourceConfig(
        id="src_wms_mysql",
        system="sys_warehouse",
        type=SourceType.SQL,
        config={
            "dialect": "mysql",
            "host": host,
            "port": mysql["port"],
            "database": "wms",
            "ssl": False,
            "query_timeout_seconds": 5,
            "batch": batch,
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": QUERY,
                    "watermark_column": "updated_at",
                    "primary_key": "id",
                }
            ],
        },
        secret_ref="local://wms-readonly",  # noqa: S106
    )
    return SqlConnector(
        source, ConnectorContext(secrets=Secrets(username, password), network=network)
    ), network


async def collect(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [record async for record in connector.read(cursor)]


async def test_select_only_login_is_verified(mysql: dict[str, Any]) -> None:
    connector, network = make(mysql, "carto_ro", RO_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert result.problems == ()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.visible == ("purchase_orders",)
    assert network.calls >= 2  # validated at every connect (ADR 0018)


async def test_insert_grant_is_refused(mysql: dict[str, Any]) -> None:
    connector, _network = make(mysql, "carto_rw", RW_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert not result.can_enable
    assert "INSERT on `wms`.`purchase_orders`" in result.problems[-1]


async def test_polling_by_watermark(mysql: dict[str, Any]) -> None:
    connector, _network = make(mysql, "carto_ro", RO_PASSWORD, batch=2)
    try:
        records = await collect(connector)
        assert [r.locator for r in records] == [f"purchase_orders:row:{n}" for n in range(1, 6)]
        assert records[0].fields is not None and records[0].fields["po_num"] == "88-210"
        assert isinstance(records[0].fields["updated_at"], datetime)
        assert records[-1].commit_cursor == {
            "watermarks": {"purchase_orders": "2026-09-23T22:04:00"}
        }
        assert await collect(connector, dict(records[-1].commit_cursor or {})) == []
    finally:
        await connector.close()


async def test_insert_through_the_connector_session_is_refused(mysql: dict[str, Any]) -> None:
    connector, _network = make(mysql, "carto_rw", RW_PASSWORD)

    def attempt() -> None:
        with connector._read_only_session() as connection:
            assert connection.execute(text("SELECT @@SESSION.max_execution_time")).scalar() == 5000
            connection.execute(text("INSERT INTO purchase_orders (po_num) VALUES ('x')"))

    try:
        with pytest.raises(DBAPIError, match="READ ONLY"):
            attempt()
        assert len(await collect(connector)) == 5
    finally:
        await connector.close()
