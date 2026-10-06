"""Every drift type in the taxonomy can be simulated by a DriftWorld and is detected by the
gateway as the right DriftEvent kind. The world is pinned to the published manifest, so a
repaired manifest can be verified against it."""
from __future__ import annotations

import pytest

from app.manifest.schema import load_manifest, load_manifest_file
from app.runtime.gateway import Connection, Gateway
from app.runtime.secrets import DictSecretsProvider
from app.verification.drift_scenarios import (
    DriftScenario,
    DriftWorld,
    EndlessPagination,
    NonJson,
    PathMoved,
    RemoveField,
    RenameField,
    RequireHeader,
    RetypeField,
    SetField,
    StatusResponse,
    Sunset,
    WrapItems,
)
from app.verification.harness import verify
from tests.conftest import ROOT

CONNECTION = Connection(config={"company_domain": "acme"})
SECRETS = DictSecretsProvider({"api_key": "k"})


def call(manifest, world, endpoint="list_employees", params=None, **kwargs):
    with Gateway(manifest, CONNECTION, SECRETS, transport=world.transport(), sleep=lambda s: None) as gateway:
        return gateway.call(endpoint, params or {}, **kwargs)


@pytest.mark.parametrize(
    "mutation, endpoint, params, kind, status, needle",
    [
        (RenameField(endpoint_id="list_employees", old="displayName", new="display_name"), "list_employees", {}, "schema_violation", 200, "'displayName' is a required property"),
        (RemoveField(endpoint_id="list_employees", field="id"), "list_employees", {}, "schema_violation", 200, "'id' is a required property"),
        (RetypeField(endpoint_id="list_employees", field="id", value=1000), "list_employees", {}, "schema_violation", 200, "is not of type 'string'"),
        (SetField(endpoint_id="get_employee", field="status", value="OnLeave"), "get_employee", {"id": "1000"}, "schema_violation", 200, "'OnLeave' is not one of"),
        (WrapItems(endpoint_id="list_employees", key="data"), "list_employees", {}, "schema_violation", 200, "'employees' is a required property"),
        (StatusResponse(endpoint_id="list_employees", status_code=401), "list_employees", {}, "unexpected_status", 401, "HTTP 401"),
        (StatusResponse(endpoint_id="list_employees", status_code=429, headers={"Retry-After": "1"}), "list_employees", {}, "unexpected_status", 429, "Retry-After: 1"),
        (NonJson(endpoint_id="list_employees"), "list_employees", {}, "malformed_body", 200, "non-JSON body"),
        (RequireHeader(name="X-Api-Key"), "list_employees", {}, "unexpected_status", 401, "X-Api-Key"),
        (PathMoved(endpoint_id="list_employees", new_path="/employees/directory/v2"), "list_employees", {}, "deprecation", 410, 'rel="successor-version"'),
    ],
    ids=["rename", "remove", "retype", "enum", "wrap", "401", "429", "non_json", "require_header", "path_moved"],
)
def test_mutation_is_detected_as_the_right_drift_kind(manifest, mutation, endpoint, params, kind, status, needle):
    world = DriftWorld(manifest, DriftScenario(mutations=[mutation]))
    result = call(manifest, world, endpoint, params)
    assert not result.ok
    assert result.status_code == status
    assert kind in {e.kind for e in result.drift_events}
    assert any(needle in e.detail for e in result.drift_events), [e.detail for e in result.drift_events]
    assert all(e.status_code == status for e in result.drift_events if e.kind == kind)


def test_removed_mapped_field_also_surfaces_as_mapping_error(manifest):
    world = DriftWorld(manifest, DriftScenario(mutations=[RemoveField(endpoint_id="list_employees", field="id")]))
    result = call(manifest, world)
    kinds = {e.kind for e in result.drift_events}
    assert kinds == {"schema_violation", "mapping_error"}
    assert result.canonical == [] and len(result.records) == 3


def test_sunset_notice_is_a_warning_not_a_failure(manifest):
    world = DriftWorld(manifest, DriftScenario(mutations=[Sunset(endpoint_id="list_employees", successor="/v2/employees")]))
    result = call(manifest, world)
    assert result.ok
    notices = [e for e in result.drift_events if e.kind == "deprecation"]
    assert len(notices) == 1 and notices[0].status_code == 200
    assert "Sunset:" in notices[0].detail and "/v2/employees" in notices[0].detail

    report = verify(manifest, mode="live", connection=CONNECTION, secrets=SECRETS, transport=world.transport(), sleep=lambda s: None)
    assert report.passed
    assert any("deprecation" in w for c in report.checks for w in c.warnings)


def test_world_serves_the_successor_path_and_retires_the_old_one(manifest):
    world = DriftWorld(manifest, DriftScenario(mutations=[PathMoved(endpoint_id="list_employees", new_path="/employees/directory/v2")]))
    assert call(manifest, world).status_code == 410

    fixed = manifest.model_copy(deep=True)
    fixed.endpoint("list_employees").path = "/employees/directory/v2"
    result = call(fixed, world)
    assert result.ok and len(result.canonical) == 3
    # Path variables carry over: the detail endpoint is untouched by the scenario.
    assert call(fixed, world, "get_employee", {"id": "1000"}).ok


def test_world_accepts_the_new_credential_and_rejects_the_old_one(manifest):
    world = DriftWorld(manifest, DriftScenario(mutations=[RequireHeader(name="X-Api-Key")]))
    assert call(manifest, world).status_code == 401
    data = manifest.model_dump(mode="json")
    data["auth"] = {"type": "api_key", "location": "header", "name": "X-Api-Key", "secret_ref": "api_key"}
    fixed = load_manifest(data)
    report = verify(fixed, mode="live", connection=CONNECTION, secrets=SECRETS, transport=world.transport(), sleep=lambda s: None)
    assert report.passed, report.summary()


def test_world_is_pinned_to_the_old_manifest_not_the_caller(manifest):
    """A candidate that only fixes its own schema still has to match what the world serves."""
    world = DriftWorld(manifest, DriftScenario(mutations=[RenameField(endpoint_id="list_employees", old="displayName", new="display_name")]))
    wrong = manifest.model_dump(mode="json")
    props = wrong["endpoints"][0]["response_schema"]["properties"]["employees"]["items"]["properties"]
    props["displayname"] = props.pop("displayName")
    wrong["endpoints"][0]["response_schema"]["properties"]["employees"]["items"]["required"] = ["id", "displayname"]
    wrong["mappings"][0]["fields"][1]["source"] = "displayname"
    result = call(load_manifest(wrong), world)
    assert not result.ok and any("'displayname' is a required property" in e.detail for e in result.drift_events)


def test_endless_pagination_is_detected_as_runaway():
    gusto = load_manifest_file(str(ROOT / "manifests" / "gusto.yaml"))
    secrets = DictSecretsProvider({"access_token": "t"})

    def paged(world, max_pages):
        with Gateway(gusto, Connection(), secrets, transport=world.transport(), sleep=lambda s: None) as gateway:
            return gateway.call("list_employees", {"company_uuid": "c1"}, max_pages=max_pages)

    healthy = paged(DriftWorld(gusto, list_size=100), max_pages=4)
    assert healthy.ok and healthy.pages == 2 and len(healthy.records) == 100

    endless = paged(DriftWorld(gusto, DriftScenario(mutations=[EndlessPagination(endpoint_id="list_employees")]), list_size=100), max_pages=4)
    assert endless.pages == 4 and len(endless.records) == 400
    assert {e.kind for e in endless.drift_events} == {"pagination_runaway"}
