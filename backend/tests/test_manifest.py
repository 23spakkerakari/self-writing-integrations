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
