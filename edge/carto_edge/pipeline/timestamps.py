"""Source timestamps: configured format or auto-detection, zone handling, UTC (spec 8.2).

Spec 8.2: "parse source timestamps with configured format or auto-detection; record timezone;
convert to UTC; if missing, use ingest time and set ``observed_at_quality=ingest``". The parser
calls :func:`find_timestamp` on structured records (the configured ``timestamp_field`` first,
then :data:`~carto_edge.config.DEFAULT_TIMESTAMP_FIELDS` in order) and :func:`leading_timestamp`
on unstructured lines; both go through :func:`parse_timestamp`.

Formats (``parse.timestamp_format``): ``iso8601`` (``Z``, numeric offsets, naive, ``T`` or
space separator, any fractional precision, basic form, ``UTC``/``GMT`` suffix), ``epoch_s``,
``epoch_ms``, any ``strftime`` pattern, or ``None`` for auto-detection, which tries ISO 8601,
epoch seconds and milliseconds (10 or 13 digits), the access-log clock (``23/Sep/2026:13:04:06
+0000``), slash dates (``2026/09/23 13:04:06``), RFC 2822 and the syslog form (``Sep 23
13:04:06``, which needs a ``reference`` datetime for the year). A value without an offset is
local time in the configured IANA zone (``parse.timezone``, resolved by :func:`resolve_zone`);
one with an offset keeps it. Every result is converted to UTC. Years outside
[:data:`MIN_YEAR`, :data:`MAX_YEAR`] are treated as unparsed: an epoch of ``0`` or a year of
``9999`` is a placeholder, not an observation. Nothing here raises on a bad value; only an
unknown zone name is an error, and that is configuration.

Every regular expression is anchored and uses bounded, non-overlapping pieces (linear time on
hostile input, spec 2.3 invariant 8), and values longer than :data:`MAX_VALUE_LEN` are
rejected before any pattern runs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from carto_edge.config import DEFAULT_TIMESTAMP_FIELDS, ParseConfig

__all__ = [
    "MAX_VALUE_LEN",
    "MAX_YEAR",
    "MIN_YEAR",
    "find_timestamp",
    "leading_timestamp",
    "parse_timestamp",
    "resolve_zone",
]

MIN_YEAR: Final = 1970
MAX_YEAR: Final = 2100
MAX_VALUE_LEN: Final = 64
"""Longest text considered a timestamp; every supported form is shorter."""

_EPOCH_MS_THRESHOLD: Final = 1e11
"""Numeric values at or above this are milliseconds (seconds this large are past 5138)."""

_MONTHS: Final = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ZONE_KEY: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]{0,31}(/[A-Za-z0-9_+\-]{1,32}){0,3}$")
_ZONE_OFFSET: Final = re.compile(r"^([+-])(\d{2}):?(\d{2})$")

_ISO_WITH_NAME: Final = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?) ?(?:UTC|GMT)$"
)
_NUMERIC: Final = re.compile(r"^\d{1,13}(?:\.\d{1,9})?$")
_CLF: Final = re.compile(
    r"^\[?(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})\]?$"
)
_SLASH: Final = re.compile(
    r"^(\d{4})/(\d{2})/(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?$"
)
_RFC2822_HINT: Final = re.compile(r"^[A-Za-z]{3}, \d{1,2} [A-Za-z]{3} \d{4} ")
_SYSLOG: Final = re.compile(r"^([A-Za-z]{3}) {1,2}(\d{1,2}) (\d{2}):(\d{2}):(\d{2})$")

_LEADING: Final = (
    re.compile(
        r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?"
        r"(?: ?(?:Z|UTC|GMT|[+-]\d{2}:?\d{2}))?)\]?:?(?=\s|$)"
    ),
    re.compile(r"^\[?(\d{1,2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]?:?(?=\s|$)"),
    re.compile(r"^\[?(\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?)\]?:?(?=\s|$)"),
    re.compile(r"^(\d{10}(?:\.\d{1,9})?|\d{13})(?=\s|$)"),
    re.compile(r"^([A-Za-z]{3} {1,2}\d{1,2} \d{2}:\d{2}:\d{2})(?=\s|$)"),
)


def resolve_zone(name: str) -> tzinfo:
    """Resolve ``parse.timezone`` to a ``tzinfo``: an IANA key, ``UTC``/``Z``/``GMT`` or a
    fixed ``+HH:MM`` offset. Raises ``ValueError`` (naming no value) for anything else."""
    key = name.strip()
    if key.upper() in {"UTC", "Z", "GMT"}:
        return UTC
    offset = _ZONE_OFFSET.match(key)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        delta = timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3)))
        if delta > timedelta(hours=14):
            msg = "timezone offset out of range"
            raise ValueError(msg)
        return timezone(sign * delta)
    if not _ZONE_KEY.match(key):
        msg = "timezone must be an IANA zone name or a +HH:MM offset"
        raise ValueError(msg)
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        msg = f"unknown timezone {key!r}"
        raise ValueError(msg) from exc


def _as_zone(zone: tzinfo | str) -> tzinfo:
    return resolve_zone(zone) if isinstance(zone, str) else zone


def _finish(value: datetime, zone: tzinfo) -> datetime | None:
    """Attach ``zone`` to a naive datetime, convert to UTC, apply the year bounds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    try:
        in_utc = value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    if in_utc.year < MIN_YEAR or in_utc.year > MAX_YEAR:
        return None
    return in_utc


def _from_epoch(number: float, *, milliseconds: bool) -> datetime | None:
    if number != number or number in (float("inf"), float("-inf")):
        return None
    seconds = number / 1000.0 if milliseconds else number
    if seconds < 0 or seconds > 4_200_000_000:
        return None
    try:
        return _finish(datetime.fromtimestamp(seconds, tz=UTC), UTC)
    except (OverflowError, ValueError, OSError):
        return None


def _parse_epoch_text(text: str, *, milliseconds: bool | None) -> datetime | None:
    """``milliseconds`` ``None`` means decide by magnitude (auto-detection)."""
    if not _NUMERIC.match(text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if milliseconds is None:
        milliseconds = number >= _EPOCH_MS_THRESHOLD
    return _from_epoch(number, milliseconds=milliseconds)


def _parse_iso(text: str, zone: tzinfo) -> datetime | None:
    try:
        return _finish(datetime.fromisoformat(text), zone)
    except ValueError:
        pass
    named = _ISO_WITH_NAME.match(text)
    if named:
        try:
            return _finish(datetime.fromisoformat(named.group(1)), UTC)
        except ValueError:
            return None
    return None


def _build(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    fraction: str | None,
    zone: tzinfo,
) -> datetime | None:
    microsecond = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        value = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=zone)
    except ValueError:
        return None
    return _finish(value, zone)


def _parse_clf(text: str) -> datetime | None:
    match = _CLF.match(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    sign = 1 if match.group(7) == "+" else -1
    offset = timedelta(hours=int(match.group(8)), minutes=int(match.group(9)))
    if offset > timedelta(hours=14):
        return None
    zone = timezone(sign * offset)
    return _build(
        int(match.group(3)),
        month,
        int(match.group(1)),
        int(match.group(4)),
        int(match.group(5)),
        int(match.group(6)),
        None,
        zone,
    )


def _parse_slash(text: str, zone: tzinfo) -> datetime | None:
    match = _SLASH.match(text)
    if not match:
        return None
    return _build(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
        int(match.group(5)),
        int(match.group(6)),
        match.group(7),
        zone,
    )


def _parse_rfc2822(text: str) -> datetime | None:
    if not _RFC2822_HINT.match(text):
        return None
    try:
        value = parsedate_to_datetime(text)
    except (ValueError, TypeError, IndexError, OverflowError):
        return None
    return _finish(value, UTC)


def _parse_syslog(text: str, zone: tzinfo, reference: datetime | None) -> datetime | None:
    match = _SYSLOG.match(text)
    if not match or reference is None:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    anchor = reference.astimezone(zone) if reference.tzinfo else reference.replace(tzinfo=zone)
    day, hour, minute, second = (int(match.group(i)) for i in range(2, 6))
    value = _build(anchor.year, month, day, hour, minute, second, None, zone)
    if value is not None and value > anchor.astimezone(UTC) + timedelta(days=1):
        value = _build(anchor.year - 1, month, day, hour, minute, second, None, zone)
    return value


def _parse_strftime(text: str, fmt: str, zone: tzinfo) -> datetime | None:
    try:
        value = datetime.strptime(text, fmt)  # noqa: DTZ007 - zone attached in _finish
    except (ValueError, TypeError, OverflowError, re.error):
        return None
    return _finish(value, zone)


def _parse_auto(text: str, zone: tzinfo, reference: datetime | None) -> datetime | None:
    first = text[0]
    if first.isdigit():
        if "-" in text:
            found = _parse_iso(text, zone)
            if found is not None:
                return found
        found = _parse_epoch_text(text, milliseconds=None)
        if found is not None:
            return found
        if "/" in text:
            found = _parse_clf(text) or _parse_slash(text, zone)
            if found is not None:
                return found
        return _parse_iso(text, zone)
    if first == "[":
        return _parse_clf(text)
    return _parse_rfc2822(text) or _parse_syslog(text, zone, reference)


def parse_timestamp(
    value: Any,
    fmt: str | None = None,
    zone: tzinfo | str = UTC,
    *,
    reference: datetime | None = None,
) -> datetime | None:
    """Parse one timestamp value to an aware UTC datetime, or ``None`` (spec 8.2).

    ``value`` is text, a number (epoch seconds, or milliseconds when at or above 10^11) or a
    ``datetime`` (naive ones are local to ``zone``). ``fmt`` is ``iso8601``, ``epoch_s``,
    ``epoch_ms``, a ``strftime`` pattern or ``None`` for auto-detection. ``reference`` supplies
    the year for syslog-style values without one (the record's receive time); without it they
    are unparsed. Booleans are not timestamps.
    """
    if value is None or isinstance(value, bool):
        return None
    tz = _as_zone(zone)
    if isinstance(value, datetime):
        return _finish(value, tz)
    if isinstance(value, int | float):
        if fmt in (None, "epoch_s", "epoch_ms", "iso8601"):
            milliseconds = fmt == "epoch_ms" if fmt in ("epoch_s", "epoch_ms") else None
            if milliseconds is None:
                milliseconds = float(value) >= _EPOCH_MS_THRESHOLD
            return _from_epoch(float(value), milliseconds=milliseconds)
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_VALUE_LEN:
        return None
    if fmt is None:
        return _parse_auto(text, tz, reference)
    if fmt == "iso8601":
        return _parse_iso(text, tz)
    if fmt == "epoch_s":
        return _parse_epoch_text(text, milliseconds=False)
    if fmt == "epoch_ms":
        return _parse_epoch_text(text, milliseconds=True)
    return _parse_strftime(text, fmt, tz)


def find_timestamp(
    fields: Mapping[str, Any],
    config: ParseConfig,
    *,
    zone: tzinfo | None = None,
    reference: datetime | None = None,
) -> tuple[datetime, str] | None:
    """The first parseable timestamp among the configured field and the defaults, with the
    path it came from so the caller can consume the field (spec 8.2)."""
    tz = zone if zone is not None else resolve_zone(config.timezone)
    names: tuple[str, ...] = DEFAULT_TIMESTAMP_FIELDS
    if config.timestamp_field:
        names = (config.timestamp_field, *DEFAULT_TIMESTAMP_FIELDS)
    for name in names:
        value = fields.get(name)
        if value is None:
            continue
        parsed = parse_timestamp(value, config.timestamp_format, tz, reference=reference)
        if parsed is not None:
            return parsed, name
    return None


def leading_timestamp(
    text: str, zone: tzinfo | str = UTC, *, reference: datetime | None = None
) -> tuple[datetime, str] | None:
    """A timestamp at the start of an unstructured line and the text after it (spec 8.2).

    Recognised: ISO 8601 with a time part (optionally in ``[]`` and followed by ``:``), the
    access-log clock, slash dates, epoch seconds or milliseconds, and the syslog form when
    ``reference`` gives the year. A bare date is not taken: in free text it is too often a
    value, not the record's clock.
    """
    tz = _as_zone(zone)
    for pattern in _LEADING:
        match = pattern.match(text)
        if not match:
            continue
        parsed = parse_timestamp(match.group(1), None, tz, reference=reference)
        if parsed is None:
            return None
        return parsed, text[match.end() :].lstrip()
    return None
