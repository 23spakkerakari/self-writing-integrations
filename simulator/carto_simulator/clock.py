"""Local time and the business calendar of scenario A (spec 10.1 calendars, 11.1 business hours).

Local time is America/New_York through :mod:`zoneinfo`, so DST is handled. The IANA database
comes from the system where one exists and from the ``tzdata`` package otherwise (a declared
dependency, ADR 0003), so every platform computes the same instants.

Everything the scenario does with time goes through this module: wall-clock construction in local
time, UTC conversion, and the business, warehouse and export calendars.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCAL_ZONE_KEY = "America/New_York"

BUSINESS_OPEN_HOUR = 8
BUSINESS_CLOSE_HOUR = 18
WAREHOUSE_OPEN_HOUR = 6
WAREHOUSE_CLOSE_HOUR = 22
EXPORT_CUTOFF_HOUR = 21
EXPORT_START = time(21, 10)
EXPECTED_FILE_BY = time(21, 30)
PICKUP_START = time(22, 0)


@cache
def local_zone() -> ZoneInfo:
    """America/New_York from the IANA database (system or the ``tzdata`` package)."""
    try:
        return ZoneInfo(LOCAL_ZONE_KEY)
    except ZoneInfoNotFoundError as exc:
        msg = (
            f"time zone {LOCAL_ZONE_KEY!r} not found: no IANA database on this platform and the "
            "tzdata package is missing; run 'uv sync --all-packages' (spec 10.1, ADR 0003)"
        )
        raise RuntimeError(msg) from exc


def local_wall(day: date, at: time = time(0), zone: tzinfo | None = None) -> datetime:
    """Aware local datetime for a wall-clock time on ``day`` (fold 0 in the ambiguous hour)."""
    return datetime.combine(day, at, tzinfo=zone or local_zone())


def local_midnight_utc(day: date, zone: tzinfo | None = None) -> datetime:
    return local_wall(day, zone=zone).astimezone(UTC)


def to_local(instant: datetime, zone: tzinfo | None = None) -> datetime:
    return instant.astimezone(zone or local_zone())


def to_utc(local: datetime) -> datetime:
    return local.astimezone(UTC)


def floor_seconds(instant: datetime) -> datetime:
    return instant.replace(microsecond=0)


def is_business_day(day: date) -> bool:
    return day.weekday() < 5


def next_business_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while not is_business_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def business_open(day: date) -> datetime:
    """08:00 local on ``day`` (which must be a business day), in UTC."""
    return to_utc(local_wall(day, time(BUSINESS_OPEN_HOUR)))


def business_close(day: date) -> datetime:
    """18:00 local on ``day``, in UTC."""
    return to_utc(local_wall(day, time(BUSINESS_CLOSE_HOUR)))


def first_business_instant(instant: datetime) -> datetime:
    """First instant at or after ``instant`` inside Mon to Fri 08:00 to 18:00 local (spec 11.1)."""
    local = to_local(instant)
    day = local.date()
    if is_business_day(day):
        if BUSINESS_OPEN_HOUR <= local.hour < BUSINESS_CLOSE_HOUR:
            return instant
        if local.hour < BUSINESS_OPEN_HOUR:
            return business_open(day)
    return business_open(next_business_day(day))


def warehouse_open(day: date) -> datetime:
    """06:00 local on ``day``, in UTC."""
    return to_utc(local_wall(day, time(WAREHOUSE_OPEN_HOUR)))


def inside_warehouse_hours(instant: datetime) -> bool:
    local = to_local(instant)
    return WAREHOUSE_OPEN_HOUR <= local.hour < WAREHOUSE_CLOSE_HOUR


def next_warehouse_opening(instant: datetime) -> datetime:
    """The next 06:00 local at or after ``instant`` (same day when before opening)."""
    local = to_local(instant)
    day = local.date() if local.hour < WAREHOUSE_OPEN_HOUR else local.date() + timedelta(days=1)
    return warehouse_open(day)


def export_day_for_pick(picked_at: datetime) -> date:
    """The local date of the nightly export a PO picked at ``picked_at`` rides (cutoff 21:00)."""
    local = to_local(picked_at)
    if local.hour < EXPORT_CUTOFF_HOUR:
        return local.date()
    return local.date() + timedelta(days=1)


def iso_offset(local: datetime) -> str:
    """``-04:00`` style offset of an aware datetime."""
    offset = local.utcoffset()
    if offset is None:
        msg = "naive datetime has no offset"
        raise ValueError(msg)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"
