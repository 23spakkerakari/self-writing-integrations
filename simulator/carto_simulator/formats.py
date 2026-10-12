"""Rendering of native record formats the edge parsers must handle (spec 8.2).

NDJSON, logfmt, one-XML-document-per-line, unstructured text, CSV and a SQL seed file. Timestamps
follow the source's convention: UTC ``Z`` with milliseconds, local time with offset, or naive
local time (spec 8.2 timestamp handling).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from xml.sax.saxutils import escape  # nosec B406  # escaping only; nothing is parsed

LogfmtValue = str | int

_LOGFMT_NEEDS_QUOTES = re.compile(r'[\s="\\]')


def iso_utc_ms(instant: datetime) -> str:
    """``2026-09-23T13:04:06.001Z`` for an aware UTC datetime."""
    return instant.isoformat(timespec="milliseconds")[:-6] + "Z"


def iso_local_offset_ms(local: datetime) -> str:
    """``2026-09-23T09:04:07.120-04:00`` for an aware local datetime."""
    return local.isoformat(timespec="milliseconds")


def naive_local_seconds(local: datetime) -> str:
    """``2026-09-23 21:12:03``: the warehouse convention, no zone, second precision."""
    return local.isoformat(sep=" ", timespec="seconds")[:19]


def ndjson_line(fields: dict[str, object]) -> str:
    return json.dumps(fields, ensure_ascii=True)


def _logfmt_value(value: LogfmtValue) -> str:
    if isinstance(value, int):
        return str(value)
    if value and _LOGFMT_NEEDS_QUOTES.search(value) is None and value.isprintable():
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def logfmt_line(pairs: Sequence[tuple[str, LogfmtValue]]) -> str:
    """``key=value`` pairs; values with spaces, quotes or ``=`` are double-quoted with escapes."""
    return " ".join(f"{key}={_logfmt_value(value)}" for key, value in pairs)


def xml_line(root: str, children: Sequence[tuple[str, str]]) -> str:
    """One complete XML document on one line, text escaped with :func:`xml.sax.saxutils.escape`."""
    body = "".join(f"<{tag}>{escape(text)}</{tag}>" for tag, text in children)
    return f"<{root}>{body}</{root}>"


def text_line(timestamp: str, level: str, message: str) -> str:
    return f"{timestamp} {level} {message}"


def csv_text(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """RFC 4180 CSV with LF line endings and a header row."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def sql_string(value: str) -> str:
    """Standard SQL string literal: single quotes doubled, no backslash escapes."""
    return "'" + value.replace("'", "''") + "'"


INSERT_TEMPLATE = "INSERT INTO {table} ({columns}) VALUES ({values});"


def sql_insert(table: str, columns: Sequence[str], values: Sequence[str]) -> str:
    """``INSERT INTO t (a, b) VALUES (1, 'x');`` with values already rendered as literals.

    This renders a seed file: nothing is executed here, ``table`` and ``columns`` are the
    scenario's own constants and every value is a rendered literal (``sql_string`` doubles
    quotes), so the SQL-injection lints (ruff S608, bandit B608) do not apply.
    """
    return INSERT_TEMPLATE.format(table=table, columns=", ".join(columns), values=", ".join(values))
