"""Canonical people-management data model.

Every integration maps its raw API records onto these objects. Flows (milestone five)
move canonical objects between integrations, so this module is the contract that
everything else depends on. Add fields conservatively; renaming one is a breaking change
for every mapping in the registry.
"""
from __future__ import annotations

from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

EmploymentStatus = Literal["active", "on_leave", "terminated", "unknown"]


class CanonicalObject(BaseModel):
    """Base for every canonical record. `source_id` is the id in the source system."""

    model_config = ConfigDict(extra="forbid")

    source_integration: str = Field(description="Manifest name of the integration the record came from")
    source_id: str = Field(min_length=1, description="Primary identifier in the source system")


class Employee(CanonicalObject):
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    work_email: str | None = None
    personal_email: str | None = None
    job_title: str | None = None
    department: str | None = None
    division: str | None = None
    location: str | None = None
    manager_source_id: str | None = Field(default=None, description="source_id of the manager, if the API exposes it")
    manager_display_name: str | None = None
    hire_date: date | None = None
    termination_date: date | None = None
    employment_status: EmploymentStatus = "unknown"
    work_phone: str | None = None
    mobile_phone: str | None = None

    # At least one of these must be populated for a record to count as a usable identity.
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = ("display_name", "work_email", "first_name", "last_name")


class Department(CanonicalObject):
    name: str
    parent_source_id: str | None = None


CANONICAL_OBJECTS: dict[str, type[CanonicalObject]] = {
    "Employee": Employee,
    "Department": Department,
}


def canonical_fields(object_name: str) -> set[str]:
    """Mappable field names for a canonical object (excludes source_integration, set by the runtime)."""
    model = CANONICAL_OBJECTS[object_name]
    return set(model.model_fields) - {"source_integration"}


def canonical_schemas() -> dict[str, dict]:
    return {name: model.model_json_schema() for name, model in CANONICAL_OBJECTS.items()}
