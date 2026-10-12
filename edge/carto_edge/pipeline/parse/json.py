"""JSON and NDJSON records (spec 8.2 item 1).

One record is one JSON object; the connector splits NDJSON into lines and JSON arrays into
elements before the parser sees them, so anything that is not an object (an array, a scalar,
trailing text) is not a record and :func:`parse_json` returns ``None``. ``NaN`` and
``Infinity`` are rejected (they are not JSON and would not round-trip to core). The standard
decoder is bounded by the record size limit the caller enforces; pathological nesting raises
``RecursionError`` inside it, which is caught like any other decode error. Flattening and its
limits are :func:`carto_edge.pipeline.parse.common.flatten`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from carto_edge.pipeline.parse.common import Flattened, flatten

__all__ = ["looks_like_json", "parse_json"]


def _reject_constant(name: str) -> None:
    msg = f"{name} is not JSON"
    raise ValueError(msg)


def looks_like_json(text: str) -> bool:
    """Cheap sniff for the auto-detector: the first non-blank character is ``{``."""
    stripped = text.lstrip()
    return stripped.startswith("{")


def parse_json(text: str) -> Flattened | None:
    """Decode one JSON object and flatten it; ``None`` when ``text`` is not a JSON object."""
    if not looks_like_json(text):
        return None
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, Mapping):
        return None
    return flatten(value)
