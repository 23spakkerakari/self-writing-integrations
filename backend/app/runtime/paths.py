"""Dotted-path access into JSON-like data: 'employees.0.workEmail'."""
from __future__ import annotations

from typing import Any

_MISSING = object()


def get_path(data: Any, path: str | None, default: Any = None) -> Any:
    if path is None or path == "":
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                current = _MISSING
        else:
            current = _MISSING
        if current is _MISSING:
            return default
    return current


def set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    current: Any = data
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.setdefault(part, {})
    if isinstance(current, list):
        current[int(parts[-1])] = value
    else:
        current[parts[-1]] = value
