"""Severity of a record (spec 8.2 "Severity", 7.1 ``severity``).

Spec 8.2: "map common level fields (``level``, ``severity``, ``log.level``) and HTTP status
classes; templates matching error lexicons (``error|exception|failed|refused|denied|timeout|
unauthorized|forbidden``) get ``severity=error`` if no explicit level." :func:`detect_severity`
applies that order: the configured ``severity_field``, then :data:`LEVEL_FIELDS` (text levels
and numeric syslog levels through :func:`map_level`), then the OpenTelemetry
``severity_number`` ranges, then :data:`STATUS_FIELDS` (``5xx`` -> error, ``4xx`` -> warn),
then the lexicon on the template text. A level field whose value maps is consumed from the
record (the event carries ``severity`` instead); a status field stays, it is an attribute.
Unknown level words are left alone and the search continues.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Final

from carto_schema.event import Severity

__all__ = [
    "LEVEL_FIELDS",
    "OTEL_NUMBER_FIELDS",
    "STATUS_FIELDS",
    "detect_severity",
    "error_lexicon_matches",
    "map_level",
    "severity_from_status",
]

LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "level",
    "severity",
    "log.level",
    "loglevel",
    "log_level",
    "lvl",
    "severity_text",
    "severityText",
    "levelname",
    "level_name",
)
"""Fields that hold an explicit level, in the order they are tried."""

OTEL_NUMBER_FIELDS: Final[tuple[str, ...]] = ("severity_number", "severityNumber")
"""OpenTelemetry numeric severities: 1-4 trace, 5-8 debug, 9-12 info, 13-16 warn, 17-20 error,
21-24 fatal."""

STATUS_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "http_status",
    "status_code",
    "httpStatus",
    "statusCode",
    "http.status_code",
    "http.response.status_code",
    "response_status",
    "response.status",
)
"""Fields that may hold an HTTP status code."""

MAX_LEVEL_LEN: Final = 16
MAX_LEXICON_TEXT: Final = 4096

_WORDS: Final[dict[str, Severity]] = {
    "trace": Severity.TRACE,
    "trc": Severity.TRACE,
    "finest": Severity.TRACE,
    "finer": Severity.TRACE,
    "verbose": Severity.TRACE,
    "debug": Severity.DEBUG,
    "dbg": Severity.DEBUG,
    "fine": Severity.DEBUG,
    "d": Severity.DEBUG,
    "info": Severity.INFO,
    "inf": Severity.INFO,
    "information": Severity.INFO,
    "informational": Severity.INFO,
    "notice": Severity.INFO,
    "i": Severity.INFO,
    "warn": Severity.WARN,
    "warning": Severity.WARN,
    "wrn": Severity.WARN,
    "w": Severity.WARN,
    "error": Severity.ERROR,
    "err": Severity.ERROR,
    "severe": Severity.ERROR,
    "e": Severity.ERROR,
    "fatal": Severity.FATAL,
    "critical": Severity.FATAL,
    "crit": Severity.FATAL,
    "emerg": Severity.FATAL,
    "emergency": Severity.FATAL,
    "alert": Severity.FATAL,
    "panic": Severity.FATAL,
    "f": Severity.FATAL,
}

_SYSLOG: Final[dict[str, Severity]] = {
    "0": Severity.FATAL,
    "1": Severity.FATAL,
    "2": Severity.FATAL,
    "3": Severity.ERROR,
    "4": Severity.WARN,
    "5": Severity.INFO,
    "6": Severity.INFO,
    "7": Severity.DEBUG,
}

_OTEL_RANGES: Final[tuple[tuple[int, Severity], ...]] = (
    (4, Severity.TRACE),
    (8, Severity.DEBUG),
    (12, Severity.INFO),
    (16, Severity.WARN),
    (20, Severity.ERROR),
    (24, Severity.FATAL),
)

_LEXICON: Final = re.compile(
    r"\b(?:error|exception|failed|refused|denied|timeout|unauthorized|forbidden)\b",
    re.IGNORECASE,
)


def map_level(value: str) -> Severity | None:
    """Map a level word (``info``, ``WARNING``, ``[ERROR]``, ``err:``, ``E``) or a numeric
    syslog level (``0`` to ``7``) to :class:`Severity`; ``None`` when unknown."""
    word = value.strip()
    if not word or len(word) > MAX_LEVEL_LEN:
        return None
    word = word.strip("[]()<>").rstrip(":").strip().lower()
    if not word:
        return None
    return _WORDS.get(word) or _SYSLOG.get(word)


def _map_otel_number(value: str) -> Severity | None:
    text = value.strip()
    if not text.isdigit() or len(text) > 2:
        return None
    number = int(text)
    if number < 1:
        return None
    for upper, severity in _OTEL_RANGES:
        if number <= upper:
            return severity
    return None


def severity_from_status(value: str) -> Severity | None:
    """``5xx`` -> error, ``4xx`` -> warn, anything else ``None`` (spec 8.2)."""
    text = value.strip()
    if len(text) != 3 or not text.isdigit():
        return None
    if text[0] == "5":
        return Severity.ERROR
    if text[0] == "4":
        return Severity.WARN
    return None


@lru_cache(maxsize=4096)
def error_lexicon_matches(template_text: str) -> bool:
    """True when the template text contains one of the spec 8.2 error words, whole-word and
    case-insensitive (``NullPointerException`` and ``errors`` do not count)."""
    return _LEXICON.search(template_text[:MAX_LEXICON_TEXT]) is not None


def detect_severity(
    fields: dict[str, str],
    template_text: str,
    *,
    severity_field: str | None = None,
    consume: bool = True,
) -> Severity | None:
    """Decide the record's severity in spec 8.2 order; consume the level field that decided."""
    names: tuple[str, ...] = LEVEL_FIELDS
    if severity_field:
        names = (severity_field, *LEVEL_FIELDS)
    for name in names:
        value = fields.get(name)
        if value is None:
            continue
        mapped = map_level(value)
        if mapped is not None:
            if consume:
                del fields[name]
            return mapped
    for name in OTEL_NUMBER_FIELDS:
        value = fields.get(name)
        if value is None:
            continue
        mapped = _map_otel_number(value)
        if mapped is not None:
            if consume:
                del fields[name]
            return mapped
    for name in STATUS_FIELDS:
        value = fields.get(name)
        if value is None:
            continue
        mapped = severity_from_status(value)
        if mapped is not None:
            return mapped
    if error_lexicon_matches(template_text):
        return Severity.ERROR
    return None
