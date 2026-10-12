"""carto_edge.connectors.splunk against an httpx MockTransport replaying the documented export
stream (spec 8.1.3, 18.2): windows, dedupe across overlapping windows, backfill chunking, the
capability check and the single POST."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorContext,
    ConnectorError,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ResolvedHost,
)
from carto_edge.connectors.splunk import SplunkConnector
from carto_edge.pipeline.model import RawRecord
from carto_schema.event import EventKind

TOKEN = "splunk-token-0123456789-secret"  # noqa: S105
NOW = datetime(2026, 10, 7, 10, 10, tzinfo=UTC)


def event(minute: int, cd: str) -> dict[str, Any]:
    when = datetime(2026, 10, 7, 10, minute, tzinfo=UTC)
    return {
        "_bkt": "orders~1~X",
        "_cd": cd,
        "_indextime": str(int(when.timestamp()) + 2),
        "_serial": "0",
        "_si": ["sh1", "orders"],
        "_sourcetype": "order_svc",
        "_time": when.isoformat(timespec="milliseconds"),
        "_raw": f"ts={when.isoformat()} level=INFO msg=order_created order_id=SO-{minute:04d}",
        "host": "orders-1",
        "source": "/var/log/order-svc.log",
        "sourcetype": "order_svc",
        "index": "orders",
        "linecount": "1",
        "splunk_server": "sh1",
        "order_id": f"SO-{minute:04d}",
    }


EVENTS = [event(0, "0:10"), event(3, "0:13"), event(6, "0:16"), event(9, "0:19")]


def locator(index: int) -> str:
    return f"splunk:{EVENTS[index]['_cd']}:{EVENTS[index]['_indextime']}"


class Secrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "vault://kv/carto/splunk-orders-token"  # noqa: S105
        return TOKEN


class Network:
    def resolve(self, host: str, port: int) -> ResolvedHost:
        return ResolvedHost(host=host, address="10.20.0.9", port=port)


class Splunk:
    """The mock search head: records every request, serves export, context and roles."""

    def __init__(
        self, capabilities: list[str] | None = None, roles: list[str] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.capabilities = (
            capabilities if capabilities is not None else ["search", "rtsearch", "list_inputs"]
        )
        self.roles = roles if roles is not None else ["carto_reader"]
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="denied")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        if path == "/services/search/jobs/export":
            assert request.method == "POST"
            form = parse_qs(request.read().decode())
            assert form["output_mode"] == ["json"]
            earliest = datetime.fromisoformat(form["earliest_time"][0])
            latest = datetime.fromisoformat(form["latest_time"][0])
            search = form["search"][0]
            lines = [json.dumps({"preview": True, "offset": 0, "result": EVENTS[0]})]
            selected = [
                e for e in EVENTS if earliest <= datetime.fromisoformat(e["_time"]) <= latest
            ]
            if search.endswith("| head 1"):
                selected = selected[:1]
            for offset, item in enumerate(selected):
                last = offset == len(selected) - 1
                lines.append(
                    json.dumps(
                        {"preview": False, "offset": offset, "lastrow": last, "result": item}
                    )
                )
            lines.append("this is not json")
            lines.append(json.dumps({"messages": [{"type": "INFO", "text": "done"}]}))
            return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
        if path == "/services/authentication/current-context":
            assert request.method == "GET"
            return httpx.Response(
                200, json={"entry": [{"content": {"username": "carto", "roles": self.roles}}]}
            )
        if path.startswith("/services/authorization/roles/"):
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "entry": [
                        {
                            "content": {
                                "capabilities": self.capabilities,
                                "imported_capabilities": ["search"],
                                "srchIndexesAllowed": ["orders"],
                                "imported_srchIndexesAllowed": ["shipping"],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)


def make(
    splunk: Splunk, config: dict[str, object] | None = None, backfill_days: int | None = None
) -> SplunkConnector:
    source = SourceConfig(
        id="src_orders_splunk",
        system="sys_orders",
        type=SourceType.SPLUNK,
        config={
            "base_url": "https://splunk.internal.example:8089",
            "search": "search index=orders sourcetype=order_svc",
            "window_minutes": 5,
            "overlap_minutes": 2,
            "max_concurrency": 2,
            **(config or {}),
        },
        secret_ref="vault://kv/carto/splunk-orders-token",  # noqa: S106
        backfill_days=backfill_days,
    )
    return SplunkConnector(
        source,
        ConnectorContext(secrets=Secrets(), network=Network()),
        transport=httpx.MockTransport(splunk),
        clock=lambda: NOW,
    )


async def collect(
    connector: SplunkConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [record async for record in connector.read(cursor)]


def test_is_a_read_connector() -> None:
    connector = make(Splunk())
    assert isinstance(connector, ReadConnector)
    assert connector.type == "splunk"


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": "https://user:pw@splunk.example:8089"},
        {"base_url": "https://splunk.example:8089/services"},
        {"base_url": "ftp://splunk.example"},
        {"base_url": "http://splunk.example:8089"},
        {"search": ""},
        {"window_minutes": 0},
        {"bogus": 1},
    ],
)
def test_invalid_configs(config: dict[str, object]) -> None:
    with pytest.raises(InvalidConfigError):
        make(Splunk(), config)


def test_needs_a_secret_ref() -> None:
    source = SourceConfig(
        id="s",
        system="sys_orders",
        type=SourceType.SPLUNK,
        config={"base_url": "https://x.example", "search": "search x"},
    )
    with pytest.raises(InvalidConfigError, match="secret_ref"):
        SplunkConnector(source, ConnectorContext(secrets=Secrets(), network=Network()))


def test_plaintext_needs_the_admin_flag(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        make(Splunk(), {"base_url": "http://splunk.example:8089", "allow_plaintext": True})
    assert any("plaintext" in record.getMessage() for record in caplog.records)


async def test_read_without_cursor_covers_the_last_window() -> None:
    splunk = Splunk()
    connector = make(splunk)
    records = await collect(connector)
    assert [record.locator for record in records] == [locator(2), locator(3)]
    first, last = records
    assert first.kind is EventKind.LOG
    assert first.text == EVENTS[2]["_raw"]
    assert first.fields == {
        "host": "orders-1",
        "source": "/var/log/order-svc.log",
        "sourcetype": "order_svc",
        "index": "orders",
        "_time": EVENTS[2]["_time"],
        "order_id": "SO-0006",
    }
    assert first.received_at == NOW
    assert first.commit_cursor is None
    assert last.commit_cursor == {"window_end": "2026-10-07T10:10:00+00:00"}
    assert first.sequence < last.sequence
    export_requests = [
        request for request in splunk.requests if request.url.path.endswith("/export")
    ]
    assert len(export_requests) == 1
    form = parse_qs(export_requests[0].read().decode())
    assert form["earliest_time"] == ["2026-10-07T10:05:00+00:00"]
    assert form["latest_time"] == ["2026-10-07T10:10:00+00:00"]
    assert form["search"] == ["search index=orders sourcetype=order_svc"]
    await connector.close()


async def test_cursor_resumes_with_overlap_and_dedupes_across_windows() -> None:
    connector = make(Splunk())
    first = await collect(connector)
    assert len(first) == 2
    # Resume from the committed window end (10:10): the overlap re-reads from 10:08, and the
    # 10:09 event is already in the dedupe set, so nothing is emitted twice.
    again = await collect(connector, dict(first[-1].commit_cursor or {}))
    assert again == []
    # A fresh instance (restart) with a cursor at 10:08 re-reads the inclusive window from
    # 10:06: both events come back and core dedupes them by event_id.
    fresh = make(Splunk())
    assert [
        r.locator for r in await collect(fresh, {"window_end": "2026-10-07T10:08:00+00:00"})
    ] == [locator(2), locator(3)]


async def test_initial_read_honours_backfill_days_in_windows() -> None:
    splunk = Splunk()
    connector = make(splunk, {"window_minutes": 60, "max_windows_per_read": 2}, backfill_days=1)
    records = await collect(connector)
    exports = [r for r in splunk.requests if r.url.path.endswith("/export")]
    assert len(exports) == 2  # capped per read
    assert records == []  # the first two hourly windows of yesterday hold nothing


async def test_backfill_chunks_with_bounded_concurrency() -> None:
    splunk = Splunk()
    connector = make(splunk, {"backfill_chunk_minutes": 60, "max_concurrency": 2})
    start = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    records = [r async for r in connector.backfill(start, start + timedelta(hours=3))]
    exports = [r for r in splunk.requests if r.url.path.endswith("/export")]
    assert len(exports) == 3
    assert [r.locator for r in records] == [locator(0), locator(1), locator(2), locator(3)]
    assert all(r.commit_cursor is None for r in records)


async def test_search_prefix_is_added() -> None:
    splunk = Splunk()
    connector = make(splunk, {"search": "index=orders"})
    await collect(connector)
    form = parse_qs(splunk.requests[0].read().decode())
    assert form["search"] == ["search index=orders"]


async def test_malformed_cursor_is_an_error() -> None:
    with pytest.raises(ConnectorError, match="window_end"):
        await collect(make(Splunk()), {"window_end": "yesterday"})


async def test_http_error_is_a_connector_error() -> None:
    splunk = Splunk()
    splunk.status = 401
    with pytest.raises(ConnectorError, match="401"):
        await collect(make(splunk))


# ---------------------------------------------------------------------------------------------
# the test method
# ---------------------------------------------------------------------------------------------


async def test_reader_role_is_verified_and_lists_indexes() -> None:
    splunk = Splunk()
    result = await make(splunk).test()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.can_enable
    assert result.visible == ("orders", "shipping")
    names = [check.name for check in result.checks]
    assert names == ["authentication", "capabilities", "search"]
    assert all(check.ok for check in result.checks)
    # Spec 8.1: the export is the only POST; everything else is GET.
    methods = {(r.method, r.url.path) for r in splunk.requests}
    posts = {path for method, path in methods if method == "POST"}
    assert posts == {"/services/search/jobs/export"}
    assert ("GET", "/services/authentication/current-context") in methods
    assert ("GET", "/services/authorization/roles/carto_reader") in methods


@pytest.mark.parametrize(
    "capability",
    [
        "edit_user",
        "delete_by_keyword",
        "admin_all_objects",
        "change_own_password",
        "restart_splunkd",
        "output_file",
    ],
)
async def test_write_capability_makes_the_token_write_capable(capability: str) -> None:
    result = await make(Splunk(capabilities=["search", capability])).test()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert not result.can_enable
    assert capability in result.problems[0]


async def test_rtsearch_is_not_a_write_capability() -> None:
    result = await make(Splunk(capabilities=["rtsearch"])).test()
    assert result.read_only is ReadOnlyStatus.VERIFIED


async def test_role_lookup_failure_is_reported_not_raised() -> None:
    splunk = Splunk()
    splunk.status = 403
    result = await make(splunk).test()
    assert not result.ok
    assert result.checks[0].name == "authentication" and not result.checks[0].ok


async def test_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        connector = make(Splunk())
        await collect(connector)
        await connector.test()
    assert TOKEN not in caplog.text
