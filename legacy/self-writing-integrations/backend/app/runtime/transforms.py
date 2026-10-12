"""Named, side-effect-free transforms that a FieldMap can apply to a raw value.

The registry is closed on purpose: the synthesis agent can only pick from these names,
which keeps generated mappings reviewable and keeps arbitrary code out of manifests.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from app.runtime.paths import get_path

Transform = Callable[[Any, dict[str, Any], dict[str, Any]], Any]
# signature: (value, args, record) -> new value

_NULL_DATES = {"", "0000-00-00", "0000-00-00 00:00:00", None}


def _identity(value: Any, args: dict, record: dict) -> Any:
    """Pass the value through unchanged."""
    return value


def _to_str(value: Any, args: dict, record: dict) -> str | None:
    """Coerce to string; None stays None."""
    if value is None:
        return None
    return str(value)


def _to_lower(value: Any, args: dict, record: dict) -> str | None:
    """Lowercase a string."""
    return None if value is None else str(value).lower()


def _to_date(value: Any, args: dict, record: dict) -> date | None:
    """Parse ISO dates and common vendor formats. Sentinel 'zero' dates become None."""
    if value in _NULL_DATES:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"cannot parse date '{text}'") from exc


def _to_bool(value: Any, args: dict, record: dict) -> bool | None:
    """Coerce common truthy strings to bool."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def _enum_map(value: Any, args: dict, record: dict) -> Any:
    """args: {"map": {"Active": "active"}, "default": "unknown", "case_insensitive": true}"""
    mapping: dict[str, Any] = args.get("map", {})
    default = args.get("default")
    if value is None:
        return default
    key = str(value)
    if args.get("case_insensitive", True):
        lowered = {str(k).lower(): v for k, v in mapping.items()}
        return lowered.get(key.lower(), default)
    return mapping.get(key, default)


def _concat(value: Any, args: dict, record: dict) -> str | None:
    """args: {"paths": ["firstName", "lastName"], "sep": " "} - joins non-empty parts from the record."""
    parts = [get_path(record, p) for p in args.get("paths", [])]
    parts = [str(p) for p in parts if p not in (None, "")]
    return args.get("sep", " ").join(parts) if parts else None


def _first_non_null(value: Any, args: dict, record: dict) -> Any:
    """args: {"paths": ["workEmail", "homeEmail"]} - first populated path in the record."""
    for p in args.get("paths", []):
        v = get_path(record, p)
        if v not in (None, ""):
            return v
    return None


def _const(value: Any, args: dict, record: dict) -> Any:
    """args: {"value": ...} - a literal."""
    return args.get("value")


def _split_take(value: Any, args: dict, record: dict) -> str | None:
    """args: {"sep": " ", "index": 0} - split a string and take one piece."""
    if value is None:
        return None
    pieces = str(value).split(args.get("sep", " "))
    idx = int(args.get("index", 0))
    try:
        return pieces[idx]
    except IndexError:
        return None


TRANSFORMS: dict[str, Transform] = {
    "identity": _identity,
    "to_str": _to_str,
    "to_lower": _to_lower,
    "to_date": _to_date,
    "to_bool": _to_bool,
    "enum_map": _enum_map,
    "concat": _concat,
    "first_non_null": _first_non_null,
    "const": _const,
    "split_take": _split_take,
}

TRANSFORM_DOCS = {name: (fn.__doc__ or "").strip() for name, fn in TRANSFORMS.items()}
