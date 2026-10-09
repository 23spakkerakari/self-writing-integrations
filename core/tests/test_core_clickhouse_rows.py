"""Row building for the spec 7.2 tables from the spec 7.1 example event, and the ClickHouse
writer against a fake client (column-oriented inserts, error wrapping, readiness)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from carto_core.db.clickhouse import (
    EVENT_COLUMNS,
    EVENT_IDENTIFIERS_TABLE,
    EVENTS_TABLE,
    IDENTIFIER_COLUMNS,
    ClickHouseWriter,
    WriterError,
    WriteResult,
    columns_of,
    event_row,
    field_ref,
    identifier_rows,
)
from carto_schema.event import CanonicalEvent

OBSERVED = datetime(2026, 10, 6, 21, 12, 3, 412000, tzinfo=UTC)
INGESTED = datetime(2026, 10, 6, 21, 12, 9, 20000, tzinfo=UTC)


class FakeClient:
    """Records inserts; optionally fails them the way clickhouse-connect does."""

    def __init__(self, *, fail: Exception | None = None, alive: bool = True) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.fail = fail
        self.alive = alive

    def insert(
        self,
        table: str,
        data: list[list[Any]],
        column_names: list[str],
        *,
        column_oriented: bool = False,
    ) -> None:
        if self.fail is not None:
            raise self.fail
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": list(column_names),
                "column_oriented": column_oriented,
            }
        )

    def ping(self) -> bool:
        if self.fail is not None:
            raise self.fail
        return self.alive


def test_event_row_reproduces_the_spec_example_column_for_column() -> None:
    row = event_row(CanonicalEvent.example())
    assert len(row) == len(EVENT_COLUMNS) == 13
    assert dict(zip(EVENT_COLUMNS, row, strict=True)) == {
        "tenant_id": "default",
        "event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0A",
        "source_id": "src_wms_db",
        "system_id": "sys_warehouse",
        "kind": "row_change",
        "observed_at": OBSERVED,
        "ingested_at": INGESTED,
        "observed_at_quality": "source",
        "template_id": "tpl_4f1c9a",
        "severity": None,
        "attributes": {"status": "CREATED", "warehouse_code": "DC-03"},
        "actor_token": "t1.Hh3Vq6Zt1Nm4Rk8Pw2Ls7D",
        "actor_kind": "human",
    }


def test_enum_columns_are_plain_strings_not_enum_members() -> None:
    row = dict(zip(EVENT_COLUMNS, event_row(CanonicalEvent.example()), strict=True))
    for column in ("kind", "observed_at_quality", "actor_kind"):
        assert type(row[column]) is str, column


def test_identifier_rows_explode_one_row_per_identifier_with_field_ref() -> None:
    rows = identifier_rows(CanonicalEvent.example())
    assert len(rows) == 4
    assert all(len(row) == len(IDENTIFIER_COLUMNS) == 8 for row in rows)
    as_dicts = [dict(zip(IDENTIFIER_COLUMNS, row, strict=True)) for row in rows]
    assert [row["field_ref"] for row in as_dicts] == [
        "sys_warehouse/tpl_4f1c9a/po_num",
        "sys_warehouse/tpl_4f1c9a/po_num",
        "sys_warehouse/tpl_4f1c9a/order_ref",
        "sys_warehouse/tpl_4f1c9a/order_ref",
    ]
    assert [row["form"] for row in as_dicts] == ["raw", "alnum", "raw", "digits.0"]
    assert [row["shape"] for row in as_dicts] == ["99-999", "99999", "AA-9999999", "9999"]
    assert as_dicts[0] == {
        "tenant_id": "default",
        "token": "t1.q8Jm0h3cR2VfZp4Lx9sT1w",
        "field_ref": "sys_warehouse/tpl_4f1c9a/po_num",
        "form": "raw",
        "shape": "99-999",
        "event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0A",
        "system_id": "sys_warehouse",
        "observed_at": OBSERVED,
    }


def test_field_ref_is_system_template_field() -> None:
    assert field_ref("sys_warehouse", "tpl_4f1c9a", "po_num") == "sys_warehouse/tpl_4f1c9a/po_num"


def test_nullable_actor_and_severity() -> None:
    bare = CanonicalEvent.model_validate(
        CanonicalEvent.example().model_dump() | {"actor": None, "severity": "error"}
    )
    row = dict(zip(EVENT_COLUMNS, event_row(bare), strict=True))
    assert row["actor_token"] is None
    assert row["actor_kind"] is None
    assert row["severity"] == "error"
    assert type(row["severity"]) is str


def test_timestamps_are_utc_aware_datetimes_at_millisecond_precision() -> None:
    row = dict(zip(EVENT_COLUMNS, event_row(CanonicalEvent.example()), strict=True))
    for column in ("observed_at", "ingested_at"):
        value = row[column]
        assert isinstance(value, datetime)
        assert value.tzinfo is UTC
        assert value.microsecond % 1000 == 0


def test_columns_of_transposes_rows_into_one_list_per_column() -> None:
    event = CanonicalEvent.example()
    other = CanonicalEvent.model_validate(
        event.model_dump() | {"event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0B"}
    )
    columns = columns_of([event_row(event), event_row(other)], len(EVENT_COLUMNS))
    assert len(columns) == 13
    assert columns[1] == ["01J9ZK8X5Q8V3N6M2T4R7W1Y0A", "01J9ZK8X5Q8V3N6M2T4R7W1Y0B"]
    assert columns_of([], len(EVENT_COLUMNS)) == [[] for _ in range(13)]


def test_writer_inserts_both_tables_column_oriented() -> None:
    client = FakeClient()
    writer = ClickHouseWriter(client)
    event = CanonicalEvent.example()
    result = writer.write_events([event, event])
    assert result == WriteResult(events=2, identifiers=8)
    assert [insert["table"] for insert in client.inserts] == [EVENTS_TABLE, EVENT_IDENTIFIERS_TABLE]
    events_insert, identifiers_insert = client.inserts
    assert events_insert["column_names"] == list(EVENT_COLUMNS)
    assert events_insert["column_oriented"] is True
    assert len(events_insert["data"]) == 13
    assert all(len(column) == 2 for column in events_insert["data"])
    assert identifiers_insert["column_names"] == list(IDENTIFIER_COLUMNS)
    assert identifiers_insert["column_oriented"] is True
    assert len(identifiers_insert["data"]) == 8
    assert all(len(column) == 8 for column in identifiers_insert["data"])


def test_writer_skips_the_identifier_insert_when_there_are_none() -> None:
    client = FakeClient()
    event = CanonicalEvent.model_validate(
        CanonicalEvent.example().model_dump() | {"identifiers": []}
    )
    assert ClickHouseWriter(client).write_events([event]) == WriteResult(events=1, identifiers=0)
    assert [insert["table"] for insert in client.inserts] == [EVENTS_TABLE]


def test_writer_with_no_events_writes_nothing() -> None:
    client = FakeClient()
    assert ClickHouseWriter(client).write_events([]) == WriteResult(events=0, identifiers=0)
    assert client.inserts == []


@pytest.mark.parametrize(
    "failure",
    [DatabaseError("Code: 241. DB::Exception: Memory limit"), OSError("connection reset")],
)
def test_writer_wraps_client_failures_without_event_contents(failure: Exception) -> None:
    writer = ClickHouseWriter(FakeClient(fail=failure))
    with pytest.raises(WriterError) as info:
        writer.write_events([CanonicalEvent.example()])
    message = str(info.value)
    assert type(failure).__name__ in message
    assert "t1." not in message
    assert "CREATED" not in message


def test_writer_ping_reports_the_client_state() -> None:
    assert ClickHouseWriter(FakeClient(alive=True)).ping() is True
    assert ClickHouseWriter(FakeClient(alive=False)).ping() is False
    assert ClickHouseWriter(FakeClient(fail=OSError("down"))).ping() is False
