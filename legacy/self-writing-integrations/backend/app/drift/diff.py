"""Structural diff between two manifests, for humans reviewing a change request.

Lists of objects that carry an identity (`id`, `endpoint_id`, `target`, `name`) are aligned by
that key so a reordered list does not read as a rewrite. Everything else is aligned by index.
"""
from __future__ import annotations

from typing import Any

_KEYS = ("id", "endpoint_id", "target", "name")


def manifest_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    _walk(before, after, path, ops)
    return ops


def _walk(a: Any, b: Any, path: str, ops: list[dict[str, Any]]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{path}/{key}"
            if key not in a:
                ops.append({"op": "add", "path": sub, "to": b[key]})
            elif key not in b:
                ops.append({"op": "remove", "path": sub, "from": a[key]})
            else:
                _walk(a[key], b[key], sub, ops)
        return
    if isinstance(a, list) and isinstance(b, list):
        key = _identity_key(a, b)
        if key is None:
            for index in range(max(len(a), len(b))):
                sub = f"{path}/{index}"
                if index >= len(a):
                    ops.append({"op": "add", "path": sub, "to": b[index]})
                elif index >= len(b):
                    ops.append({"op": "remove", "path": sub, "from": a[index]})
                else:
                    _walk(a[index], b[index], sub, ops)
            return
        left = {item[key]: item for item in a}
        right = {item[key]: item for item in b}
        for ident in list(left) + [k for k in right if k not in left]:
            sub = f"{path}/[{key}={ident}]"
            if ident not in right:
                ops.append({"op": "remove", "path": sub, "from": left[ident]})
            elif ident not in left:
                ops.append({"op": "add", "path": sub, "to": right[ident]})
            else:
                _walk(left[ident], right[ident], sub, ops)
        return
    if a != b:
        ops.append({"op": "replace", "path": path or "/", "from": a, "to": b})


def _identity_key(a: list[Any], b: list[Any]) -> str | None:
    items = a + b
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for key in _KEYS:
        values = [i.get(key) for i in items]
        if all(v is not None for v in values) and len(set(values[: len(a)])) == len(a) and len(set(values[len(a) :])) == len(b):
            return key
    return None


def summarize(ops: list[dict[str, Any]], limit: int = 12) -> str:
    lines = []
    for op in ops[:limit]:
        if op["op"] == "replace":
            lines.append(f"{op['path']}: {op['from']!r} -> {op['to']!r}")
        elif op["op"] == "add":
            lines.append(f"{op['path']}: added")
        else:
            lines.append(f"{op['path']}: removed")
    if len(ops) > limit:
        lines.append(f"... and {len(ops) - limit} more")
    return "\n".join(lines) or "no changes"
