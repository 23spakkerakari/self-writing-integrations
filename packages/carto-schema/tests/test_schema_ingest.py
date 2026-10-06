"""IngestBatch and SourceHeartbeat: the internal endpoint bodies (spec 8.1, 8.5, 12)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from carto_schema.event import CanonicalEvent
from carto_schema.ingest import (
    MAX_EVENTS_PER_BATCH,
    MAX_HEARTBEAT_MESSAGE_LEN,
    IngestBatch,
    SourceHeartbeat,
    SourceStatus,
)

BATCH_ID = "01J9ZK8X5Q8V3N6M2T4R7W1Y0B"


def batch_dict(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "tenant_id": "default",
        "source_id": "src_wms_db",
        "batch_id": BATCH_ID,
        "sent_at": "2026-10-06T21:12:10.000Z",
        "events": [CanonicalEvent.example().model_dump(mode="json")],
        **overrides,
    }


def heartbeat_dict(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "tenant_id": "default",
        "source_id": "src_wms_db",
        "sent_at": "2026-10-06T21:12:10.000Z",
        "status": "ok",
        "last_success_at": "2026-10-06T21:12:09.020Z",
        "lag_seconds": 6.0,
        "error_count": 0,
        "buffer_depth": 12,
        "oldest_buffered_at": "2026-10-06T21:11:58.500Z",
        "message": "",
        **overrides,
    }


def error_types(info: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, tuple[Any, ...]]]:
    return [(error["type"], tuple(error["loc"])) for error in info.value.errors()]


# -- IngestBatch ----------------------------------------------------------------------------------


def test_batch_validates_and_round_trips() -> None:
    batch = IngestBatch.model_validate(batch_dict())
    assert batch.events == [CanonicalEvent.example()]
    assert batch.sent_at == datetime(2026, 10, 6, 21, 12, 10, tzinfo=UTC)
    text = batch.model_dump_json()
    assert IngestBatch.model_validate_json(text) == batch
    assert IngestBatch.model_validate_json(text).model_dump_json() == text
    assert IngestBatch.model_validate(batch.model_dump(mode="json")) == batch
    dumped = json.loads(text)
    assert list(dumped) == [
        "schema_version",
        "tenant_id",
        "source_id",
        "batch_id",
        "sent_at",
        "events",
    ]
    assert dumped["sent_at"] == "2026-10-06T21:12:10.000Z"
    assert dumped["events"] == [json.loads(CanonicalEvent.example().model_dump_json())]


def test_batch_size_limits() -> None:
    assert MAX_EVENTS_PER_BATCH == 5000
    event = CanonicalEvent.example()
    full = IngestBatch.model_validate(batch_dict(events=[event] * MAX_EVENTS_PER_BATCH))
    assert len(full.events) == MAX_EVENTS_PER_BATCH

    with pytest.raises(ValidationError) as info:
        IngestBatch.model_validate(batch_dict(events=[event] * (MAX_EVENTS_PER_BATCH + 1)))
    assert error_types(info) == [("too_long", ("events",))]

    with pytest.raises(ValidationError) as info:
        IngestBatch.model_validate(batch_dict(events=[]))
    assert error_types(info) == [("too_short", ("events",))]


def test_batch_rejects_events_of_another_tenant() -> None:
    foreign = CanonicalEvent.example().model_dump(mode="json") | {"tenant_id": "other"}
    with pytest.raises(ValidationError, match=r"events\[1\] belongs to tenant 'other'") as info:
        IngestBatch.model_validate(batch_dict(events=[*batch_dict()["events"], foreign]))
    assert error_types(info) == [("value_error", ())]


def test_batch_rejects_events_of_another_source() -> None:
    foreign = CanonicalEvent.example().model_dump(mode="json") | {"source_id": "src_orders_log"}
    with pytest.raises(ValidationError, match=r"events\[0\] belongs to .* 'src_orders_log'"):
        IngestBatch.model_validate(batch_dict(events=[foreign]))


def test_batch_header_rules() -> None:
    for bad in (
        {"schema_version": "2"},
        {"batch_id": "not-a-ulid"},
        {"tenant_id": "Default"},
        {"source_id": "src-wms"},
        {"sent_at": "2026-10-06T21:12:10"},
        {"compression": "zstd"},
    ):
        with pytest.raises(ValidationError):
            IngestBatch.model_validate(batch_dict(**bad))


def test_batch_reports_invalid_events_with_their_index() -> None:
    broken = CanonicalEvent.example().model_dump(mode="json") | {"event_id": "bad"}
    with pytest.raises(ValidationError) as info:
        IngestBatch.model_validate(batch_dict(events=[*batch_dict()["events"], broken]))
    assert error_types(info) == [("string_pattern_mismatch", ("events", 1, "event_id"))]


# -- SourceHeartbeat ------------------------------------------------------------------------------


def test_heartbeat_validates_and_round_trips() -> None:
    heartbeat = SourceHeartbeat.model_validate(heartbeat_dict())
    assert heartbeat.status is SourceStatus.OK
    assert heartbeat.last_success_at == datetime(2026, 10, 6, 21, 12, 9, 20000, tzinfo=UTC)
    text = heartbeat.model_dump_json()
    assert SourceHeartbeat.model_validate_json(text) == heartbeat
    assert SourceHeartbeat.model_validate_json(text).model_dump_json() == text
    assert json.loads(text) == heartbeat_dict()


def test_heartbeat_statuses() -> None:
    assert [s.value for s in SourceStatus] == ["ok", "degraded", "failing", "paused"]
    for status in SourceStatus:
        assert SourceHeartbeat.model_validate(heartbeat_dict(status=status.value)).status is status
    with pytest.raises(ValidationError):
        SourceHeartbeat.model_validate(heartbeat_dict(status="broken"))


def test_heartbeat_nullable_timestamps_and_default_message() -> None:
    data = heartbeat_dict(last_success_at=None, oldest_buffered_at=None)
    del data["message"]
    heartbeat = SourceHeartbeat.model_validate(data)
    assert heartbeat.last_success_at is None
    assert heartbeat.oldest_buffered_at is None
    assert heartbeat.message == ""
    dumped = json.loads(heartbeat.model_dump_json())
    assert dumped["last_success_at"] is None
    assert dumped["oldest_buffered_at"] is None
    assert dumped["message"] == ""


def test_heartbeat_timestamp_rule_applies_to_optional_fields() -> None:
    with pytest.raises(ValidationError) as info:
        SourceHeartbeat.model_validate(heartbeat_dict(last_success_at="2026-10-06T21:12:09"))
    assert error_types(info) == [("timezone_aware", ("last_success_at",))]
    heartbeat = SourceHeartbeat.model_validate(
        heartbeat_dict(oldest_buffered_at="2026-10-06T17:11:58.500999-04:00")
    )
    assert heartbeat.oldest_buffered_at == datetime(2026, 10, 6, 21, 11, 58, 500000, tzinfo=UTC)
    assert json.loads(heartbeat.model_dump_json())["oldest_buffered_at"] == (
        "2026-10-06T21:11:58.500Z"
    )


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("lag_seconds", -0.5, "greater_than_equal"),
        ("lag_seconds", math.nan, "finite_number"),
        ("lag_seconds", math.inf, "finite_number"),
        ("lag_seconds", "soon", "float_type"),
        ("lag_seconds", True, "float_type"),
        ("error_count", -1, "greater_than_equal"),
        ("error_count", 1.5, "int_type"),
        ("error_count", "3", "int_type"),
        ("error_count", True, "int_type"),
        ("buffer_depth", -1, "greater_than_equal"),
        ("buffer_depth", False, "int_type"),
        ("message", "m" * (MAX_HEARTBEAT_MESSAGE_LEN + 1), "string_too_long"),
        ("schema_version", "0", "literal_error"),
        ("sent_at", None, "value_error"),
        ("sent_at", 1700000000, "value_error"),
        ("last_success_at", 1700000000.5, "value_error"),
    ],
)
def test_heartbeat_field_rules(key: str, value: object, error: str) -> None:
    with pytest.raises(ValidationError) as info:
        SourceHeartbeat.model_validate(heartbeat_dict(**{key: value}))
    assert error_types(info) == [(error, (key,))]


def test_heartbeat_accepts_boundaries_and_integer_lag() -> None:
    heartbeat = SourceHeartbeat.model_validate(
        heartbeat_dict(lag_seconds=0, error_count=0, buffer_depth=0, message="m" * 1024)
    )
    assert heartbeat.lag_seconds == 0.0
    assert len(heartbeat.message) == MAX_HEARTBEAT_MESSAGE_LEN
    from_json = SourceHeartbeat.model_validate_json(json.dumps(heartbeat_dict(lag_seconds=7)))
    assert from_json.lag_seconds == 7.0


def test_heartbeat_numbers_are_strict_in_json_mode_too() -> None:
    # The exported schema says integer/number; a JSON boolean or string must not coerce.
    for key, value in (("error_count", True), ("buffer_depth", "12"), ("lag_seconds", "6.0")):
        with pytest.raises(ValidationError) as info:
            SourceHeartbeat.model_validate_json(json.dumps(heartbeat_dict(**{key: value})))
        assert [error["loc"] for error in info.value.errors()] == [(key,)]


def test_heartbeat_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError) as info:
        SourceHeartbeat.model_validate(heartbeat_dict(cursor="file:3"))
    assert error_types(info) == [("extra_forbidden", ("cursor",))]
