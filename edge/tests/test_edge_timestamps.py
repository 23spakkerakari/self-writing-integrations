"""Timestamp parsing: configured format or auto-detection, zone handling, UTC (spec 8.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.config import DEFAULT_TIMESTAMP_FIELDS, ParseConfig
from carto_edge.pipeline.timestamps import (
    MAX_YEAR,
    MIN_YEAR,
    find_timestamp,
    leading_timestamp,
    parse_timestamp,
    resolve_zone,
)

NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-23T13:04:06.001Z", datetime(2026, 9, 23, 13, 4, 6, 1000, tzinfo=UTC)),
        ("2026-09-23T13:04:06Z", datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)),
        ("2026-09-23T09:04:07.120-04:00", datetime(2026, 9, 23, 13, 4, 7, 120000, tzinfo=UTC)),
        ("2026-09-23T09:04:07+0530", datetime(2026, 9, 23, 3, 34, 7, tzinfo=UTC)),
        ("2026-09-23T13:04:06.123456789Z", datetime(2026, 9, 23, 13, 4, 6, 123456, tzinfo=UTC)),
        ("2026-09-23 13:04:06", datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)),
        ("2026-09-23 13:04:06.5", datetime(2026, 9, 23, 13, 4, 6, 500000, tzinfo=UTC)),
        ("2026-09-23T13:04", datetime(2026, 9, 23, 13, 4, tzinfo=UTC)),
        ("2026-09-23", datetime(2026, 9, 23, tzinfo=UTC)),
        ("2026-09-23 13:04:06 +0000", datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)),
        ("2026-09-23 13:04:06 UTC", datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)),
        ("2026-09-23T13:04:06,250Z", datetime(2026, 9, 23, 13, 4, 6, 250000, tzinfo=UTC)),
    ],
)
def test_timestamps_iso8601_variants(value: str, expected: datetime) -> None:
    assert parse_timestamp(value, "iso8601") == expected
    assert parse_timestamp(value) == expected


def test_timestamps_naive_values_get_the_configured_zone() -> None:
    assert parse_timestamp("2026-09-23 21:12:03", None, NEW_YORK) == datetime(
        2026, 9, 24, 1, 12, 3, tzinfo=UTC
    )
    assert parse_timestamp("2026-09-23 21:12:03", "iso8601", "America/New_York") == datetime(
        2026, 9, 24, 1, 12, 3, tzinfo=UTC
    )
    winter = parse_timestamp("2026-01-15 21:12:03", None, NEW_YORK)
    assert winter == datetime(2026, 1, 16, 2, 12, 3, tzinfo=UTC)


def test_timestamps_offsets_win_over_the_configured_zone() -> None:
    assert parse_timestamp("2026-09-23T13:04:06Z", None, NEW_YORK) == datetime(
        2026, 9, 23, 13, 4, 6, tzinfo=UTC
    )


def test_timestamps_result_is_always_utc() -> None:
    parsed = parse_timestamp("2026-09-23T09:04:07-04:00")
    assert parsed is not None
    assert parsed.tzinfo is UTC
    parsed = parse_timestamp("2026-09-23 09:04:07", None, NEW_YORK)
    assert parsed is not None
    assert parsed.tzinfo is UTC


def test_timestamps_epoch_seconds_and_milliseconds() -> None:
    assert parse_timestamp("1790000000", "epoch_s") == datetime.fromtimestamp(1790000000, tz=UTC)
    assert parse_timestamp(1790000000, "epoch_s") == datetime.fromtimestamp(1790000000, tz=UTC)
    assert parse_timestamp("1790000000.5", "epoch_s") == datetime.fromtimestamp(
        1790000000.5, tz=UTC
    )
    assert parse_timestamp("1790000000123", "epoch_ms") == datetime.fromtimestamp(
        1790000000.123, tz=UTC
    )
    assert parse_timestamp(1790000000123, "epoch_ms") == datetime.fromtimestamp(
        1790000000.123, tz=UTC
    )
    assert parse_timestamp("1790000000") == datetime.fromtimestamp(1790000000, tz=UTC)
    assert parse_timestamp("1790000000123") == datetime.fromtimestamp(1790000000.123, tz=UTC)
    assert parse_timestamp(1790000000.25) == datetime.fromtimestamp(1790000000.25, tz=UTC)
    assert parse_timestamp("abc", "epoch_s") is None
    assert parse_timestamp("1e400", "epoch_s") is None
    assert parse_timestamp("nan", "epoch_s") is None
    assert parse_timestamp(True) is None


def test_timestamps_strftime_patterns() -> None:
    assert parse_timestamp("23/09/2026 13:04:06", "%d/%m/%Y %H:%M:%S", NEW_YORK) == datetime(
        2026, 9, 23, 17, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("23/Sep/2026:13:04:06 -0400", "%d/%b/%Y:%H:%M:%S %z") == datetime(
        2026, 9, 23, 17, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("nope", "%d/%m/%Y") is None
    assert parse_timestamp("23/09/2026", "%Q") is None


def test_timestamps_auto_detects_common_non_iso_formats() -> None:
    assert parse_timestamp("23/Sep/2026:13:04:06 +0000") == datetime(
        2026, 9, 23, 13, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("[23/Sep/2026:13:04:06 +0000]") == datetime(
        2026, 9, 23, 13, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("2026/09/23 13:04:06") == datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)
    assert parse_timestamp("Wed, 23 Sep 2026 13:04:06 +0000") == datetime(
        2026, 9, 23, 13, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("Wed, 23 Sep 2026 13:04:06 GMT") == datetime(
        2026, 9, 23, 13, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("20260923T130406Z") == datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)
    reference = datetime(2026, 10, 8, tzinfo=UTC)
    assert parse_timestamp("Sep 23 13:04:06", reference=reference) == datetime(
        2026, 9, 23, 13, 4, 6, tzinfo=UTC
    )
    assert parse_timestamp("Sep 23 13:04:06") is None
    assert parse_timestamp(
        "Dec 31 23:59:59", reference=datetime(2027, 1, 1, tzinfo=UTC)
    ) == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


def test_timestamps_accept_datetime_objects_and_reject_junk() -> None:
    aware = datetime(2026, 9, 23, 9, 4, 7, tzinfo=timezone(timedelta(hours=-4)))
    assert parse_timestamp(aware) == datetime(2026, 9, 23, 13, 4, 7, tzinfo=UTC)
    naive = datetime(2026, 9, 23, 21, 12, 3)  # a naive input on purpose
    assert parse_timestamp(naive, None, NEW_YORK) == datetime(2026, 9, 24, 1, 12, 3, tzinfo=UTC)
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("   ") is None
    assert parse_timestamp("not a date") is None
    assert parse_timestamp("2026-13-45T99:99:99Z") is None
    assert parse_timestamp("x" * 10_000) is None
    assert parse_timestamp(["2026-09-23"]) is None
    assert parse_timestamp({"a": 1}) is None


def test_timestamps_reject_absurd_years() -> None:
    assert parse_timestamp("1969-12-31T23:59:59Z") is None
    assert parse_timestamp("2101-01-01T00:00:00Z") is None
    assert parse_timestamp(f"{MIN_YEAR}-01-01T00:00:00Z") is not None
    assert parse_timestamp(f"{MAX_YEAR}-12-31T23:59:59Z") is not None
    assert parse_timestamp("-1", "epoch_s") is None
    assert parse_timestamp("99999999999", "epoch_s") is None
    assert parse_timestamp("0001-01-01T00:00:00Z") is None


def test_timestamps_resolve_zone() -> None:
    assert resolve_zone("UTC") is UTC
    assert resolve_zone("utc") is UTC
    assert resolve_zone("Z") is UTC
    assert resolve_zone("America/New_York").key == "America/New_York"  # type: ignore[attr-defined]
    assert resolve_zone("+02:00").utcoffset(None) == timedelta(hours=2)
    with pytest.raises(ValueError, match="timezone"):
        resolve_zone("Mars/Olympus")
    with pytest.raises(ValueError, match="timezone"):
        resolve_zone("../etc/passwd")


def test_timestamps_find_configured_field_first_then_defaults() -> None:
    config = ParseConfig(timestamp_field="when", timezone="America/New_York")
    fields = {"when": "2026-09-23 21:12:03", "ts": "2026-09-23T13:04:06Z"}
    found = find_timestamp(fields, config)
    assert found == (datetime(2026, 9, 24, 1, 12, 3, tzinfo=UTC), "when")
    found = find_timestamp({"ts": "2026-09-23T13:04:06Z", "time": "x"}, ParseConfig())
    assert found == (datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC), "ts")
    found = find_timestamp({"@timestamp": "2026-09-23T13:04:06Z"}, ParseConfig())
    assert found is not None
    assert found[1] == "@timestamp"
    found = find_timestamp({"created_at": "2026-09-23 09:21:04"}, ParseConfig())
    assert found == (datetime(2026, 9, 23, 9, 21, 4, tzinfo=UTC), "created_at")


def test_timestamps_find_skips_unparseable_candidates_and_reports_none() -> None:
    assert find_timestamp({"ts": "garbage", "time": "2026-09-23T13:04:06Z"}, ParseConfig()) == (
        datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC),
        "time",
    )
    assert find_timestamp({"msg": "x"}, ParseConfig()) is None
    assert find_timestamp({}, ParseConfig()) is None
    assert (
        find_timestamp({"ts": "2026-09-23T13:04:06Z"}, ParseConfig(timestamp_format="epoch_s"))
        is None
    )


def test_timestamps_find_uses_the_configured_format_and_zone() -> None:
    config = ParseConfig(timestamp_format="%d/%m/%Y %H:%M:%S", timezone="America/New_York")
    assert find_timestamp({"ts": "23/09/2026 21:12:03"}, config) == (
        datetime(2026, 9, 24, 1, 12, 3, tzinfo=UTC),
        "ts",
    )
    assert find_timestamp({"ts": "23/09/2026 21:12:03"}, config, zone=UTC) == (
        datetime(2026, 9, 23, 21, 12, 3, tzinfo=UTC),
        "ts",
    )


def test_timestamps_default_fields_cover_the_simulator_and_rows() -> None:
    assert {"ts", "timestamp", "created_at"} <= set(DEFAULT_TIMESTAMP_FIELDS)


def test_timestamps_leading_timestamp_styles() -> None:
    cases = {
        "2026-09-23 21:12:41 INFO x": (datetime(2026, 9, 24, 1, 12, 41, tzinfo=UTC), "INFO x"),
        "2026-09-23T13:04:06.001Z x": (datetime(2026, 9, 23, 13, 4, 6, 1000, tzinfo=UTC), "x"),
        "2026-09-23T09:04:07-04:00 x": (datetime(2026, 9, 23, 13, 4, 7, tzinfo=UTC), "x"),
        "[2026-09-23 21:12:41] x": (datetime(2026, 9, 24, 1, 12, 41, tzinfo=UTC), "x"),
        "[2026-09-23T21:12:41.5Z]: x": (datetime(2026, 9, 23, 21, 12, 41, 500000, tzinfo=UTC), "x"),
        "2026-09-23 21:12:41,123 x": (datetime(2026, 9, 24, 1, 12, 41, 123000, tzinfo=UTC), "x"),
        "1790000000 x": (datetime.fromtimestamp(1790000000, tz=UTC), "x"),
        "1790000000.250 x": (datetime.fromtimestamp(1790000000.25, tz=UTC), "x"),
        "1790000000123 x": (datetime.fromtimestamp(1790000000.123, tz=UTC), "x"),
        "23/Sep/2026:13:04:06 +0000 x": (datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC), "x"),
        "2026/09/23 21:12:41 x": (datetime(2026, 9, 24, 1, 12, 41, tzinfo=UTC), "x"),
    }
    for text, expected in cases.items():
        assert leading_timestamp(text, NEW_YORK) == expected, text


def test_timestamps_leading_timestamp_absent_or_bogus() -> None:
    assert leading_timestamp("INFO started", NEW_YORK) is None
    assert leading_timestamp("", NEW_YORK) is None
    assert leading_timestamp("2026-13-45 99:99:99 x", NEW_YORK) is None
    assert leading_timestamp("4471 orders", NEW_YORK) is None
    assert leading_timestamp("2026-09-23T21:12:41Z", NEW_YORK) == (
        datetime(2026, 9, 23, 21, 12, 41, tzinfo=UTC),
        "",
    )


def test_timestamps_leading_syslog_needs_a_reference_year() -> None:
    assert leading_timestamp("Sep 23 21:12:41 host x", UTC) is None
    found = leading_timestamp(
        "Sep 23 21:12:41 host x", UTC, reference=datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert found == (datetime(2026, 9, 23, 21, 12, 41, tzinfo=UTC), "host x")


@settings(max_examples=300, deadline=2000)
@given(st.text(max_size=100))
def test_timestamps_property_parse_never_raises_and_is_utc(text: str) -> None:
    parsed = parse_timestamp(text, None, NEW_YORK)
    assert parsed is None or (parsed.tzinfo is UTC and MIN_YEAR <= parsed.year <= MAX_YEAR)
    found = leading_timestamp(text, NEW_YORK)
    assert found is None or found[0].tzinfo is UTC


@settings(max_examples=100, deadline=2000)
@given(st.datetimes(min_value=datetime(1970, 1, 2), max_value=datetime(2100, 12, 30)))
def test_timestamps_property_iso_round_trip(value: datetime) -> None:
    aware = value.replace(tzinfo=UTC)
    assert parse_timestamp(aware.isoformat()) == aware
    assert parse_timestamp(aware.isoformat().replace("+00:00", "Z")) == aware
    assert parse_timestamp(aware.strftime("%Y-%m-%d %H:%M:%S"), None, UTC) == aware.replace(
        microsecond=0
    )
