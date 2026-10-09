"""Helpers shared by the record parsers (spec 8.2, 2.3 invariant 8).

:func:`flatten` turns a nested mapping (a JSON object, an OTLP body, a webhook payload, a DB
row) into the flat ``path -> string`` mapping of :class:`~carto_edge.pipeline.model.ParsedRecord`:
nested keys become dotted paths (``payload.order.id``), arrays are indexed up to
:data:`MAX_ARRAY_ITEMS` elements, nesting stops at :data:`MAX_DEPTH` and the number of fields at
:data:`MAX_FIELDS` (spec 8.2 item 1). Every limit that cuts something off leaves a note so the
pipeline can count it; nothing raises. :func:`stringify` fixes how scalars are rendered so the
same value gives the same string whichever parser produced it (tokens are HMACs of these strings,
spec 8.4). :func:`key_signature` builds the ``keys:<sorted top-level keys>`` template of
structured records without a message (ADR 0016).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final
from uuid import UUID

__all__ = [
    "MAX_ARRAY_ITEMS",
    "MAX_DEPTH",
    "MAX_FIELDS",
    "MAX_KEY_LEN",
    "MAX_SIGNATURE_KEYS",
    "NOTE_ARRAY_LIMIT",
    "NOTE_DEPTH_LIMIT",
    "NOTE_FIELD_LIMIT",
    "Flattened",
    "flatten",
    "key_signature",
    "stringify",
    "utf8_len",
]

MAX_DEPTH: Final = 12
"""Spec 8.2 item 1: nesting levels kept; deeper values are dropped with a note."""
MAX_ARRAY_ITEMS: Final = 20
"""Spec 8.2 item 1: array elements kept per array."""
MAX_FIELDS: Final = 1024
"""Fields kept per record; the canonical event allows 256 attributes and 64 identifiers."""
MAX_KEY_LEN: Final = 256
"""Longest path kept (``MAX_FIELD_NAME_LEN`` of the canonical event)."""
MAX_SIGNATURE_KEYS: Final = 32
"""Keys listed in a ``keys:`` template (ADR 0016)."""
MAX_SIGNATURE_KEY_LEN: Final = 64

NOTE_DEPTH_LIMIT: Final = "depth_limit"
NOTE_ARRAY_LIMIT: Final = "array_limit"
NOTE_FIELD_LIMIT: Final = "field_limit"
NOTE_KEY_LIMIT: Final = "key_limit"


@dataclass(frozen=True, slots=True)
class Flattened:
    """A flattened record: ``fields`` by path, the top-level keys in input order, limit notes."""

    fields: dict[str, str]
    top_keys: tuple[str, ...]
    notes: tuple[str, ...]


def utf8_len(text: str) -> int:
    """Number of UTF-8 bytes of ``text`` (``parse.max_record_bytes`` counts bytes, spec 8.2)."""
    return len(text.encode("utf-8", errors="surrogatepass"))


def stringify(value: Any) -> str | None:
    """Render a scalar as the string the pipeline works on; ``None`` for null.

    Strings stay as they are; booleans become ``true``/``false`` (checked before integers,
    since ``bool`` is an ``int``); numbers use ``str``; ``datetime`` and ``date`` use ISO
    8601; ``bytes`` are hex; ``Decimal``, ``UUID`` and enums use their natural text. Anything
    else falls back to ``str``, bounded by the caller's size limit.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, Enum):
        return stringify(value.value)
    return str(value)


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def flatten(
    value: Mapping[Any, Any],
    *,
    max_depth: int = MAX_DEPTH,
    max_items: int = MAX_ARRAY_ITEMS,
    max_fields: int = MAX_FIELDS,
) -> Flattened:
    """Flatten a nested mapping to dotted paths (spec 8.2 item 1).

    Iterative (no recursion, so adversarial nesting cannot exhaust the stack): a work stack of
    ``(prefix, depth, value)``. Mappings contribute ``prefix.key``; sequences contribute
    ``prefix.<index>`` for the first ``max_items`` elements; scalars are rendered with
    :func:`stringify` and nulls are skipped. Values nested deeper than ``max_depth`` levels,
    elements past ``max_items`` and fields past ``max_fields`` are dropped and noted. Keys are
    rendered with ``str`` (JSON keys are strings already; DB rows may use other types); paths
    longer than :data:`MAX_KEY_LEN` are dropped and noted, and a scalar under an empty key
    (``{"": 1}``) is dropped since a field needs a name. Field order follows a
    depth-first walk in input order, so ``top_keys`` and the ``fields`` order are stable.
    """
    fields: dict[str, str] = {}
    notes: set[str] = set()
    top_keys = tuple(str(key) for key in value)
    if len(value) <= max_fields and not any(
        isinstance(child, Mapping | list | tuple) for child in value.values()
    ):
        # Fast path: a flat record of scalars (the common NDJSON and row shape).
        for key, child in value.items():
            text = stringify(child)
            path = str(key)
            if text is None or not path:
                continue
            if len(path) > MAX_KEY_LEN:
                notes.add(NOTE_KEY_LIMIT)
                continue
            fields[path] = text
        return Flattened(fields=fields, top_keys=top_keys, notes=tuple(sorted(notes)))
    stack: list[tuple[str, int, Any]] = [("", 0, value)]
    while stack:
        prefix, depth, current = stack.pop()
        if isinstance(current, Mapping):
            if depth >= max_depth:
                notes.add(NOTE_DEPTH_LIMIT)
                continue
            children: list[tuple[str, int, Any]] = [
                (_join(prefix, str(key)), depth + 1, child) for key, child in current.items()
            ]
            stack.extend(reversed(children))
        elif isinstance(current, list | tuple):
            if depth >= max_depth:
                notes.add(NOTE_DEPTH_LIMIT)
                continue
            if len(current) > max_items:
                notes.add(NOTE_ARRAY_LIMIT)
            items = [
                (_join(prefix, str(index)), depth + 1, child)
                for index, child in enumerate(current[:max_items])
            ]
            stack.extend(reversed(items))
        else:
            text = stringify(current)
            if text is None or not prefix:
                continue
            if len(prefix) > MAX_KEY_LEN:
                notes.add(NOTE_KEY_LIMIT)
                continue
            if len(fields) >= max_fields and prefix not in fields:
                notes.add(NOTE_FIELD_LIMIT)
                continue
            fields[prefix] = text
    return Flattened(fields=fields, top_keys=top_keys, notes=tuple(sorted(notes)))


def _signature_key(key: str) -> str:
    """One key as it appears in a ``keys:`` template: whitespace, commas and control
    characters become ``_`` so the template stays one line of comma-separated names."""
    cleaned = "".join("_" if (c.isspace() or c == "," or not c.isprintable()) else c for c in key)
    return cleaned[:MAX_SIGNATURE_KEY_LEN]


def key_signature(keys: Iterable[str]) -> str:
    """``keys:<sorted top-level keys>`` (ADR 0016), at most :data:`MAX_SIGNATURE_KEYS` keys."""
    unique = sorted({_signature_key(key) for key in keys})
    return "keys:" + ",".join(unique[:MAX_SIGNATURE_KEYS])
