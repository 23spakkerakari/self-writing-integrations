"""Apply a manifest Mapping to raw records, producing validated canonical objects."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.canonical.people import CANONICAL_OBJECTS, CanonicalObject
from app.manifest.schema import Mapping
from app.runtime.paths import get_path
from app.runtime.transforms import TRANSFORMS


class MappingError(ValueError):
    pass


def map_record(mapping: Mapping, record: Any, integration_name: str) -> CanonicalObject:
    if not isinstance(record, dict):
        raise MappingError(f"record is {type(record).__name__}, expected object")
    model = CANONICAL_OBJECTS[mapping.canonical_object]
    values: dict[str, Any] = {"source_integration": integration_name}
    for fm in mapping.fields:
        value = get_path(record, fm.source) if fm.source is not None else None
        if fm.transform is not None:
            try:
                value = TRANSFORMS[fm.transform](value, fm.args, record)
            except Exception as exc:  # transform errors are data errors, not crashes
                raise MappingError(f"{fm.target}: transform '{fm.transform}' failed: {exc}") from exc
        if fm.target == "source_id" and value is not None:
            value = str(value)
        values[fm.target] = value
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise MappingError(problems) from exc


def map_records(
    mapping: Mapping, records: list[Any], integration_name: str
) -> tuple[list[CanonicalObject], list[str]]:
    """Map every record; collect per-record errors instead of failing the whole batch."""
    out: list[CanonicalObject] = []
    errors: list[str] = []
    for index, record in enumerate(records):
        try:
            out.append(map_record(mapping, record, integration_name))
        except MappingError as exc:
            errors.append(f"record[{index}]: {exc}")
    return out, errors
