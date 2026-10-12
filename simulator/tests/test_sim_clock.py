"""Local time and the business calendar (spec 10.1, 11.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from carto_simulator import clock

ZONE = clock.local_zone()


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_local_zone_is_new_york() -> None:
    zone = clock.local_zone()
    assert isinstance(zone, ZoneInfo)
    assert str(zone) == "America/New_York"
    assert clock.local_zone() is zone


def test_local_zone_names_tzdata_when_the_database_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(key: str) -> ZoneInfo:
        raise ZoneInfoNotFoundError(key)

    clock.local_zone.cache_clear()
    monkeypatch.setattr(clock, "ZoneInfo", missing)
    try:
        with pytest.raises(RuntimeError, match="tzdata"):
            clock.local_zone()
    finally:
        clock.local_zone.cache_clear()
    monkeypatch.undo()
    assert str(clock.local_zone()) == "America/New_York"


@pytest.mark.parametrize(
    ("instant", "expected", "name"),
    [
        ("2026-03-08T06:59:59", "2026-03-08T01:59:59-05:00", "EST"),
        ("2026-03-08T07:00:00", "2026-03-08T03:00:00-04:00", "EDT"),
        ("2026-11-01T05:59:59", "2026-11-01T01:59:59-04:00", "EDT"),
        ("2026-11-01T06:00:00", "2026-11-01T01:00:00-05:00", "EST"),
        ("2026-09-23T13:04:06", "2026-09-23T09:04:06-04:00", "EDT"),
    ],
)
def test_zone_transitions_in_the_simulated_year(instant: str, expected: str, name: str) -> None:
    local = utc(instant).astimezone(ZONE)
    assert local.isoformat() == expected
    assert local.tzname() == name


def test_gap_and_fold_follow_pep_495() -> None:
    gap = datetime(2026, 3, 8, 2, 30, tzinfo=ZONE)
    assert gap.astimezone(UTC) == utc("2026-03-08T07:30:00")
    assert gap.replace(fold=1).astimezone(UTC) == utc("2026-03-08T06:30:00")
    ambiguous = datetime(2026, 11, 1, 1, 30, tzinfo=ZONE)
    assert ambiguous.astimezone(UTC) == utc("2026-11-01T05:30:00")
    assert ambiguous.replace(fold=1).astimezone(UTC) == utc("2026-11-01T06:30:00")


def test_business_calendar() -> None:
    saturday = utc("2026-09-26T15:00:00")
    assert (
        clock.to_local(clock.first_business_instant(saturday)).isoformat()
        == "2026-09-28T08:00:00-04:00"
    )
    early = utc("2026-09-23T05:00:00")  # 01:00 local on a Wednesday
    assert (
        clock.to_local(clock.first_business_instant(early)).isoformat()
        == "2026-09-23T08:00:00-04:00"
    )
    inside = utc("2026-09-23T15:00:00")
    assert clock.first_business_instant(inside) == inside
    late = utc("2026-09-25T23:30:00")  # Friday 19:30 local
    assert clock.to_local(clock.first_business_instant(late)).date() == date(2026, 9, 28)
    assert clock.next_business_day(date(2026, 9, 25)) == date(2026, 9, 28)
    assert clock.business_open(date(2026, 9, 23)) == utc("2026-09-23T12:00:00")
    assert clock.business_close(date(2026, 9, 23)) == utc("2026-09-23T22:00:00")
    assert clock.is_business_day(date(2026, 9, 27)) is False


def test_warehouse_calendar_and_export_cutoff() -> None:
    before_opening = utc("2026-09-23T07:30:00")  # 03:30 local
    assert clock.inside_warehouse_hours(before_opening) is False
    assert (
        clock.to_local(clock.next_warehouse_opening(before_opening)).isoformat()
        == "2026-09-23T06:00:00-04:00"
    )
    after_closing = utc("2026-09-24T02:30:00")  # 22:30 local on the 23rd
    assert (
        clock.to_local(clock.next_warehouse_opening(after_closing)).isoformat()
        == "2026-09-24T06:00:00-04:00"
    )
    assert clock.inside_warehouse_hours(utc("2026-09-23T15:00:00")) is True
    assert clock.export_day_for_pick(utc("2026-09-24T00:59:00")) == date(2026, 9, 23)  # 20:59 local
    assert clock.export_day_for_pick(utc("2026-09-24T01:00:00")) == date(2026, 9, 24)  # 21:00 local


def test_local_wall_and_offsets() -> None:
    assert clock.local_wall(date(2026, 9, 23), time(21, 10)).astimezone(UTC) == utc(
        "2026-09-24T01:10:00"
    )
    assert clock.local_midnight_utc(date(2026, 12, 1)) == utc("2026-12-01T05:00:00")
    assert clock.iso_offset(clock.to_local(utc("2026-09-23T12:00:00"))) == "-04:00"
    assert clock.iso_offset(clock.to_local(utc("2026-12-23T12:00:00"))) == "-05:00"
    assert clock.iso_offset(utc("2026-12-23T12:00:00")) == "+00:00"
    assert clock.floor_seconds(utc("2026-09-23T12:00:00.750")).microsecond == 0
    with pytest.raises(ValueError, match="naive"):
        clock.iso_offset(datetime(2026, 1, 1))
