"""Unstructured text lines (spec 8.2 item 6, before template mining).

A text line is ``[timestamp] [level] message``: :func:`parse_text` takes a leading timestamp
when there is one (:func:`~carto_edge.pipeline.timestamps.leading_timestamp`), then a level
token that :func:`~carto_edge.pipeline.severity.map_level` recognises (``INFO``, ``[WARN]``,
``error:``), skipping ``-`` and ``|`` separators between the parts, and the rest is the message
the template miner works on. A line that leaves no message (a timestamp alone) is not a
record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Final

from carto_edge.pipeline.severity import map_level
from carto_edge.pipeline.timestamps import leading_timestamp

__all__ = ["TextRecord", "parse_text"]

_LEVEL_TOKEN: Final = re.compile(r"^[\[(<]?([A-Za-z]{1,12})[\])>]?:?(?=\s|$)")
_SEPARATOR: Final = re.compile(r"^[-|:]+\s+")


@dataclass(frozen=True, slots=True)
class TextRecord:
    """The message to mine, the source timestamp if the line carried one, the level token."""

    message: str
    timestamp: datetime | None
    level: str | None


def _skip_separator(text: str) -> str:
    match = _SEPARATOR.match(text)
    return text[match.end() :] if match else text


def parse_text(
    text: str, zone: tzinfo = UTC, *, reference: datetime | None = None
) -> TextRecord | None:
    """Split one line into timestamp, level and message; ``None`` when no message remains."""
    line = text.strip()
    if not line:
        return None
    timestamp: datetime | None = None
    found = leading_timestamp(line, zone, reference=reference)
    rest = line
    if found is not None:
        timestamp, rest = found
    rest = _skip_separator(rest)
    level: str | None = None
    match = _LEVEL_TOKEN.match(rest)
    if match is not None and map_level(match.group(1)) is not None:
        level = match.group(1)
        rest = _skip_separator(rest[match.end() :].lstrip())
    message = rest.strip()
    if not message:
        return None
    return TextRecord(message=message, timestamp=timestamp, level=level)
