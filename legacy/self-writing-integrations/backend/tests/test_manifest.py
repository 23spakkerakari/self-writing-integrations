import copy

import pytest
from pydantic import ValidationError

from app.manifest.schema import BasicAuth, load_manifest


def test_reference_manifest_loads(manifest):
    assert manifest.name == "bamboohr"
    assert isinstance(manifest.auth, BasicAuth)
    assert {e.id for e in manifest.endpoints} == {"list_employees", "get_employee"}
    assert manifest.secret_refs() == ["api_key"]
    assert manifest.mapping_for("list_employees").canonical_object == "Employee"


def test_unknown_canonical_target_rejected(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["mappings"][0]["fields"].append({"target": "favourite_colour", "source": "colour"})
    with pytest.raises(ValidationError, match="not a field of Employee"):
        load_manifest(data)


def test_mapping_must_include_source_id(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["mappings"][0]["fields"] = [f for f in data["mappings"][0]["fields"] if f["target"] != "source_id"]
    with pytest.raises(ValidationError, match="must map source_id"):
        load_manifest(data)


def test_unknown_transform_rejected(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["mappings"][0]["fields"][0]["transform"] = "run_python"
    with pytest.raises(ValidationError, match="unknown transform"):
        load_manifest(data)


def test_duplicate_endpoint_ids_rejected(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["endpoints"].append(copy.deepcopy(data["endpoints"][0]))
    with pytest.raises(ValidationError, match="unique"):
        load_manifest(data)


def test_undeclared_path_variable_rejected(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["endpoints"][1]["parameters"] = [p for p in data["endpoints"][1]["parameters"] if p["name"] != "id"]
    with pytest.raises(ValidationError, match="not declared as a path parameter"):
        load_manifest(data)


def test_base_url_variable_must_be_config_var(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["config_vars"] = []
    with pytest.raises(ValidationError, match="config_vars"):
        load_manifest(data)


def test_basic_auth_needs_exactly_one_username_source(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["auth"] = {"type": "basic", "password_literal": "x"}
    with pytest.raises(ValidationError, match="username"):
        load_manifest(data)


def test_extra_keys_rejected(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["generated_code"] = "print('hi')"
    with pytest.raises(ValidationError):
        load_manifest(data)


def test_oauth_manifest_loads_and_computes_scopes():
    from tests.conftest import ROOT
    from app.manifest.schema import OAuth2Auth, load_manifest_file

    gusto = load_manifest_file(str(ROOT / "manifests" / "gusto.yaml"))
    assert isinstance(gusto.auth, OAuth2Auth)
    assert gusto.secret_refs() == ["access_token"]
    assert gusto.required_scopes() == ["companies:read", "employees:read"]
    assert gusto.required_scopes(["get_employee"]) == ["companies:read", "employees:read"]
    assert gusto.required_scopes([]) == ["companies:read"]


# --- milestone 6, step 1: named types, direction, responses by status -------------------------


def served_manifest_dict() -> dict:
    return {
        "name": "payroll",
        "display_name": "Payroll service",
        "base_url": "http://payroll.internal",
        "auth": {"type": "none"},
        "types": {
            "Employee": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
            },
            "Error": {"type": "object", "required": ["detail"], "properties": {"detail": {"type": "string"}}},
        },
        "endpoints": [
            {
                "id": "list_employees",
                "method": "GET",
                "path": "/employees",
                "direction": "served",
                "response_schema": {"type": "array", "items": {"$ref": "#/$defs/Employee"}},
                "responses": {"401": {"$ref": "#/$defs/Error"}},
                "response_headers": [{"name": "X-Request-Id", "required": True}],
            },
            {
                "id": "get_employee",
                "method": "GET",
                "path": "/employees/{employee_id}",
                "direction": "served",
                "parameters": [{"name": "employee_id", "location": "path", "required": True}],
                "response_schema": {"$ref": "#/$defs/Employee", "description": "One employee"},
                "responses": {"404": {"$ref": "#/$defs/Error"}},
            },
        ],
    }


def test_type_references_resolve_into_endpoint_schemas():
    m = load_manifest(served_manifest_dict())
    one = m.resolve(m.endpoint("get_employee").response_schema)
    assert "$ref" not in one
    assert one["properties"]["id"] == {"type": "string"}
    assert one["description"] == "One employee"
    many = m.resolve(m.endpoint("list_employees").response_schema)
    assert many["items"]["required"] == ["id", "name"]


def test_body_schema_picks_the_declared_shape_for_a_status():
    m = load_manifest(served_manifest_dict())
    ep = m.endpoint("get_employee")
    assert m.body_schema(ep, 404)["required"] == ["detail"]
    assert m.body_schema(ep, 200)["required"] == ["id", "name"]
    assert m.body_schema(ep, 500) is None


def test_direction_defaults_to_called(manifest):
    assert all(e.direction == "called" for e in manifest.endpoints)
    assert manifest.served_endpoints() == []
    served = load_manifest(served_manifest_dict())
    assert [e.id for e in served.served_endpoints()] == ["list_employees", "get_employee"]


def test_unknown_type_reference_rejected():
    data = served_manifest_dict()
    data["endpoints"][1]["response_schema"] = {"$ref": "#/$defs/Person"}
    with pytest.raises(ValidationError, match="unknown type 'Person'"):
        load_manifest(data)


def test_remote_reference_rejected():
    data = served_manifest_dict()
    data["types"]["Employee"]["properties"]["manager"] = {"$ref": "https://example.com/manager.json"}
    with pytest.raises(ValidationError, match="only local type references"):
        load_manifest(data)


def test_type_name_must_be_an_identifier():
    data = served_manifest_dict()
    data["types"]["Employee Record"] = {"type": "object"}
    with pytest.raises(ValidationError, match="must be an identifier"):
        load_manifest(data)


def test_responses_keys_must_be_status_codes():
    data = served_manifest_dict()
    data["endpoints"][0]["responses"] = {"unauthorised": {"type": "object"}}
    with pytest.raises(ValidationError, match="three-digit HTTP status code"):
        load_manifest(data)


def test_recursive_type_stays_resolvable():
    from jsonschema import Draft202012Validator, ValidationError as SchemaError

    data = served_manifest_dict()
    data["types"]["Department"] = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "string"}, "parent": {"anyOf": [{"$ref": "#/$defs/Department"}, {"type": "null"}]}},
    }
    data["endpoints"].append(
        {"id": "get_department", "method": "GET", "path": "/department", "response_schema": {"$ref": "#/$defs/Department"}}
    )
    m = load_manifest(data)
    resolved = m.resolve(m.endpoint("get_department").response_schema)
    assert resolved["properties"]["id"] == {"type": "string"}
    assert "Department" in resolved["$defs"]
    validator = Draft202012Validator(resolved)
    validator.validate({"id": "1", "parent": {"id": "0", "parent": None}})
    with pytest.raises(SchemaError):
        validator.validate({"id": "1", "parent": {"parent": None}})


def test_manifest_with_types_round_trips():
    m = load_manifest(served_manifest_dict())
    again = load_manifest(m.model_dump(mode="json"))
    assert again.types == m.types
    assert again.endpoint("get_employee").responses == {"404": {"$ref": "#/$defs/Error"}}
    assert again.endpoint("list_employees").response_headers[0].name == "X-Request-Id"
