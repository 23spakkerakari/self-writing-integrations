from datetime import date

import pytest

from app.manifest.schema import FieldMap, Mapping
from app.runtime.mapping import MappingError, map_record, map_records
from app.runtime.paths import get_path, set_path
from app.runtime.transforms import TRANSFORMS


def test_get_and_set_path():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert get_path(data, "a.b.1.c") == 2
    assert get_path(data, "a.x.c", default="none") == "none"
    assert get_path(data, None) is data
    set_path(data, "a.d", 3)
    assert data["a"]["d"] == 3


def test_to_date_handles_sentinels_and_formats():
    to_date = TRANSFORMS["to_date"]
    assert to_date("0000-00-00", {}, {}) is None
    assert to_date("", {}, {}) is None
    assert to_date("2020-01-15", {}, {}) == date(2020, 1, 15)
    assert to_date("2020-01-15T09:30:00Z", {}, {}) == date(2020, 1, 15)
    with pytest.raises(ValueError):
        to_date("yesterday", {}, {})


def test_enum_map_is_case_insensitive_with_default():
    fn = TRANSFORMS["enum_map"]
    args = {"map": {"Active": "active", "Inactive": "terminated"}, "default": "unknown"}
    assert fn("ACTIVE", args, {}) == "active"
    assert fn("Retired", args, {}) == "unknown"
    assert fn(None, args, {}) == "unknown"


def test_concat_and_first_non_null_read_from_record():
    record = {"firstName": "Ada", "lastName": "Lovelace", "workEmail": None, "homeEmail": "ada@home.example"}
    assert TRANSFORMS["concat"](None, {"paths": ["firstName", "lastName"]}, record) == "Ada Lovelace"
    assert TRANSFORMS["first_non_null"](None, {"paths": ["workEmail", "homeEmail"]}, record) == "ada@home.example"


def test_map_record_produces_canonical_employee():
    mapping = Mapping(
        endpoint_id="x",
        canonical_object="Employee",
        fields=[
            FieldMap(target="source_id", source="id"),
            FieldMap(target="display_name", transform="concat", args={"paths": ["firstName", "lastName"]}),
            FieldMap(target="hire_date", source="hireDate", transform="to_date"),
            FieldMap(target="employment_status", source="status", transform="enum_map", args={"map": {"Active": "active"}, "default": "unknown"}),
        ],
    )
    emp = map_record(mapping, {"id": 7, "firstName": "Ada", "lastName": "Lovelace", "hireDate": "2020-01-15", "status": "Active"}, "test")
    assert emp.source_id == "7"
    assert emp.display_name == "Ada Lovelace"
    assert emp.hire_date == date(2020, 1, 15)
    assert emp.employment_status == "active"
    assert emp.source_integration == "test"


def test_map_records_collects_errors_per_record():
    mapping = Mapping(endpoint_id="x", canonical_object="Employee", fields=[FieldMap(target="source_id", source="id")])
    good, errors = map_records(mapping, [{"id": "1"}, {"nope": 1}, "not-an-object"], "test")
    assert [g.source_id for g in good] == ["1"]
    assert len(errors) == 2
    assert errors[0].startswith("record[1]")


def test_map_record_reports_transform_failure():
    mapping = Mapping(
        endpoint_id="x",
        canonical_object="Employee",
        fields=[FieldMap(target="source_id", source="id"), FieldMap(target="hire_date", source="hireDate", transform="to_date")],
    )
    with pytest.raises(MappingError, match="hire_date"):
        map_record(mapping, {"id": "1", "hireDate": "soon"}, "test")
