"""Deterministic repairs for drift that has exactly one mechanical fix.

Only field renames are patched without the model: a required field is missing from the observed
body and exactly one unknown field of a compatible type appeared in its place. The schema,
items_path, pagination paths and mapping sources are rewritten together so the candidate stays
consistent. Anything ambiguous is left to the repair agent, and every candidate, mechanical or
not, still has to pass verification against the API as it behaves now.
"""
from __future__ import annotations

import re
from typing import Any

from app.drift.models import DriftIncident
from app.manifest.schema import IntegrationManifest, load_manifest
from app.runtime.paths import get_path

_REQUIRED = re.compile(r"^(?P<location>[^:]+): '(?P<field>[^']+)' is a required property$")
_JSON_TYPES = {
    str: {"string"},
    bool: {"boolean"},
    int: {"integer", "number"},
    float: {"number"},
    list: {"array"},
    dict: {"object"},
    type(None): {"null"},
}


def deterministic_repair(manifest: IntegrationManifest, incident: DriftIncident) -> tuple[IntegrationManifest, str] | None:
    """Return a patched manifest and a description, or None when no mechanical fix applies."""
    if incident.kind != "schema_violation" or incident.sample_body is None:
        return None
    try:
        endpoint = manifest.endpoint(incident.endpoint_id)
    except KeyError:
        return None
    if endpoint.response_schema is None:
        return None

    # Three records reporting the same missing field are one schema problem, so group by the
    # schema location (indexes collapsed) and keep one concrete record as the sample.
    missing_by_container: dict[str, tuple[str, set[str]]] = {}
    for sample in incident.samples:
        match = _REQUIRED.match(sample)
        if match:
            location = match.group("location")
            key = "/".join("*" if part.isdigit() else part for part in location.split("/"))
            concrete, fields = missing_by_container.setdefault(key, (location, set()))
            fields.add(match.group("field"))
    if not missing_by_container:
        return None

    data = manifest.model_dump(mode="json")
    ep_data = next(e for e in data["endpoints"] if e["id"] == endpoint.id)
    renames: list[tuple[str, str, str]] = []
    for location, missing in missing_by_container.values():
        container = get_path(incident.sample_body, _dotted(location))
        schema = _schema_at(ep_data["response_schema"], location)
        if not isinstance(container, dict) or schema is None or not isinstance(schema.get("properties"), dict):
            return None
        props = schema["properties"]
        extras = {k: v for k, v in container.items() if k not in props}
        pairing = _pair(missing, extras, props)
        if pairing is None:
            return None
        for old, new in pairing.items():
            props[new] = props.pop(old)
            schema["required"] = [new if r == old else r for r in schema.get("required", [])]
            renames.append((location, old, new))

    for location, old, new in renames:
        _rewrite_paths(ep_data, data["mappings"], location, old, new)
    try:
        patched = load_manifest(data)
    except ValueError:
        return None
    description = ", ".join(f"rename {old}->{new}" + ("" if loc == "$" else f" at {loc}") for loc, old, new in renames)
    return patched, description


# --- helpers ------------------------------------------------------------------------------


def _pair(missing: set[str], extras: dict[str, Any], props: dict[str, Any]) -> dict[str, str] | None:
    """Match each missing field to exactly one type-compatible unknown field, or give up."""
    if len(extras) < len(missing):
        return None
    pairing: dict[str, str] = {}
    claimed: set[str] = set()
    for old in sorted(missing):
        candidates = [k for k, v in extras.items() if k not in claimed and _compatible(props.get(old, {}), v)]
        if len(candidates) != 1:
            return None
        pairing[old] = candidates[0]
        claimed.add(candidates[0])
    return pairing


def _compatible(schema: dict[str, Any], value: Any) -> bool:
    declared = schema.get("type")
    if declared is None:
        return True
    allowed = set(declared) if isinstance(declared, list) else {declared}
    return bool(_JSON_TYPES.get(type(value), set()) & allowed)


def _schema_at(root: dict[str, Any], location: str) -> dict[str, Any] | None:
    schema: Any = root
    for part in location.split("/"):
        if part in ("$", ""):
            continue
        if not isinstance(schema, dict):
            return None
        schema = schema.get("items", {}) if part.isdigit() else schema.get("properties", {}).get(part)
        if schema is None:
            return None
    return schema if isinstance(schema, dict) else None


def _dotted(location: str) -> str:
    return "" if location == "$" else location.replace("/", ".")


def _rewrite_paths(ep_data: dict[str, Any], mappings: list[dict[str, Any]], location: str, old: str, new: str) -> None:
    body_old = f"{_dotted(location)}.{old}".lstrip(".")
    body_new = f"{_dotted(location)}.{new}".lstrip(".")
    if ep_data.get("items_path"):
        ep_data["items_path"] = _swap(ep_data["items_path"], body_old, body_new)
    pagination = ep_data.get("pagination") or {}
    if pagination.get("next_cursor_path"):
        pagination["next_cursor_path"] = _swap(pagination["next_cursor_path"], body_old, body_new)

    relative = _relative_to_record(location, ep_data.get("items_path"))
    if relative is None:
        return
    record_old = f"{relative}.{old}".lstrip(".")
    record_new = f"{relative}.{new}".lstrip(".")
    for mapping in mappings:
        if mapping["endpoint_id"] != ep_data["id"]:
            continue
        for field in mapping["fields"]:
            if field.get("source"):
                field["source"] = _swap(field["source"], record_old, record_new)
            paths = field.get("args", {}).get("paths")
            if isinstance(paths, list):
                field["args"]["paths"] = [_swap(str(p), record_old, record_new) for p in paths]


def _relative_to_record(location: str, items_path: str | None) -> str | None:
    """Location of the container relative to one record, or None when it is body-level."""
    if not items_path:
        return "" if location == "$" else _dotted(location)
    prefix = items_path.replace(".", "/") + "/"
    if not location.startswith(prefix):
        return None
    rest = location[len(prefix) :]
    index, _, remainder = rest.partition("/")
    if not index.isdigit():
        return None
    return remainder.replace("/", ".")


def _swap(path: str, old: str, new: str) -> str:
    if path == old:
        return new
    if path.startswith(old + "."):
        return new + path[len(old) :]
    return path
