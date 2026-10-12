"""The drift loop over HTTP in mock gateway mode: inject drift at the provider, watch the
incident open from live calls, repair, approve, canary, promote, roll back."""
from __future__ import annotations

import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.routes import create_app
from app.config import Settings
from app.manifest.schema import load_manifest
from app.synthesis.agent import SynthesisResult
from tests.conftest import ROOT
from tests.test_oauth_api import connect

CONNECTION = {"config": {"company_domain": "acme"}, "secrets": {"api_key": "dev-key"}}
RENAME = {"type": "rename_field", "endpoint_id": "list_employees", "old": "displayName", "new": "display_name"}


@pytest.fixture
def api(tmp_path, manifest_dict):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'drift-api.db'}",
        gateway_mode="mock",
        canary_min_calls=3,
        canary_fraction=0.5,
        public_base_url="https://platform.example",
    )
    with TestClient(create_app(settings)) as client:
        assert client.post("/integrations/import", json={"manifest": manifest_dict}).status_code == 201
        assert client.post("/integrations/bamboohr/versions/0.1.0/verify", json={"mode": "mock"}).json()["passed"]
        assert client.post("/integrations/bamboohr/versions/0.1.0/publish").status_code == 200
        yield client


def call(api: TestClient, endpoint="list_employees", params=None):
    response = api.post("/integrations/bamboohr/call", json={"endpoint_id": endpoint, "params": params or {}, "connection": CONNECTION})
    assert response.status_code == 200, response.text
    return response.json()


def inject(api: TestClient, *mutations, integration="bamboohr"):
    response = api.post(f"/mock/drift/{integration}", json={"scenario": {"name": "test", "mutations": list(mutations)}})
    assert response.status_code == 200, response.text
    return response.json()


def test_full_drift_loop_over_http(api):
    assert call(api)["ok"]
    assert api.get("/drift/incidents").json() == []

    world = inject(api, RENAME)
    assert world["pinned_version"] == "0.1.0"
    assert api.get("/mock/drift").json()[0]["integration"] == "bamboohr"

    assert not call(api)["ok"]
    incidents = api.get("/drift/incidents", params={"active": True}).json()
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["kind"] == "schema_violation" and inc["status"] == "open" and inc["count"] == 1
    assert api.get(f"/drift/incidents/{inc['id']}").json()["tenant_id"] == "default"

    triage = api.post(f"/drift/incidents/{inc['id']}/triage").json()["triage"]
    assert (triage["drift_class"], triage["risk_class"], triage["repairable"]) == ("schema", "high", True)
    assert "displayName" in triage["rationale"]

    repaired = api.post(f"/drift/incidents/{inc['id']}/repair", json={})
    assert repaired.status_code == 200, repaired.text
    outcome = repaired.json()
    assert outcome["strategy"].startswith("deterministic:rename")
    change = outcome["change_request"]
    assert change["status"] == "pending" and change["candidate_version"] == "0.1.1"
    assert api.get("/changes", params={"status": "pending"}).json()[0]["id"] == change["id"]
    assert api.get(f"/changes/{change['id']}").json()["diff"]
    assert api.get("/integrations").json()[0]["published_version"] == "0.1.0"

    assert api.post(f"/changes/{change['id']}/promote").status_code == 409  # not approved
    assert api.post(f"/changes/{change['id']}/canary").status_code == 409

    approved = api.post(f"/changes/{change['id']}/approve", json={"actor": "pradhi", "note": "ok"}).json()
    assert approved["status"] == "approved" and approved["decided_by"] == "pradhi"
    assert api.post(f"/changes/{change['id']}/canary", json={"fraction": 1.0}).json()["status"] == "canary"
    for _ in range(3):
        assert call(api)["ok"]  # every call is routed to the candidate at fraction 1.0
    report = api.get(f"/changes/{change['id']}/canary").json()
    assert report["verdict"] == "pass" and report["candidate"]["calls"] == 3 and report["candidate"]["failures"] == 0

    promoted = api.post(f"/changes/{change['id']}/promote", json={"actor": "pradhi"}).json()
    assert promoted["status"] == "promoted"
    assert api.get("/integrations").json()[0]["published_version"] == "0.1.1"
    assert api.get(f"/drift/incidents/{inc['id']}").json()["status"] == "resolved"
    assert call(api)["ok"]

    rolled = api.post("/integrations/bamboohr/rollback", json={"actor": "pradhi", "note": "drill"})
    assert rolled.status_code == 200 and rolled.json()["version"] == "0.1.0"
    assert api.get(f"/changes/{change['id']}").json()["status"] == "rolled_back"
    assert api.post("/integrations/bamboohr/rollback", json={}).status_code == 409  # nothing left to roll back to

    assert api.delete("/mock/drift/bamboohr").status_code == 200
    assert api.delete("/mock/drift/bamboohr").status_code == 404
    assert call(api)["ok"]


def test_policy_and_worker_tick_over_http(api):
    assert api.put("/integrations/bamboohr/approval-policy", json={"risk_class": "medium", "auto_approve": True}).json()["auto_approve"]["medium"]
    assert api.get("/integrations/bamboohr/approval-policy").json()["auto_approve"] == {"low": False, "medium": True, "high": False}

    inject(api, {"type": "wrap_items", "endpoint_id": "list_employees", "key": "data"})
    assert not call(api)["ok"]
    inc_id = api.get("/drift/incidents").json()[0]["id"]

    tick = api.post("/drift/tick").json()
    assert tick["repaired"] == [inc_id] and len(tick["canaries_started"]) == 1
    change = api.get("/changes").json()[0]
    assert change["status"] == "canary" and change["auto_approved"]
    for _ in range(12):
        call(api)
    tick = api.post("/drift/tick").json()
    assert tick["promoted"] == [change["id"]], tick
    assert api.get("/integrations").json()[0]["published_version"] == "0.1.1"


def test_agent_repair_over_http_with_an_injected_agent(api):
    inject(api, {"type": "set_field", "endpoint_id": "get_employee", "field": "status", "value": "OnLeave"})
    assert not call(api, "get_employee", {"id": "1000"})["ok"]
    inc = api.get("/drift/incidents").json()[0]

    def fake_agent(spec_text, name_hint, model, max_attempts, client, previous, feedback):
        data = copy.deepcopy(previous.model_dump(mode="json"))
        endpoint = next(e for e in data["endpoints"] if e["id"] == "get_employee")
        endpoint["response_schema"]["properties"]["status"]["enum"].append("OnLeave")
        mapping = next(m for m in data["mappings"] if m["endpoint_id"] == "get_employee")
        next(f for f in mapping["fields"] if f["target"] == "employment_status")["args"]["map"]["OnLeave"] = "on_leave"
        return SynthesisResult(manifest=load_manifest(data), attempts=1, transcript=[feedback])

    api.app.state.pipeline.repair_fn = fake_agent
    outcome = api.post(f"/drift/incidents/{inc['id']}/repair", json={"connection": CONNECTION}).json()
    assert outcome["strategy"] == "agent:claude-opus-5"
    assert outcome["triage"]["drift_class"] == "semantic"
    assert outcome["change_request"]["status"] == "pending"


def test_dismiss_and_spec_check(api):
    inject(api, {"type": "sunset", "endpoint_id": "list_employees"})
    assert call(api)["ok"]
    inc = api.get("/drift/incidents").json()[0]
    assert inc["kind"] == "deprecation"
    dismissed = api.post(f"/drift/incidents/{inc['id']}/dismiss", json={"actor": "pradhi", "note": "migration planned"}).json()
    assert dismissed["status"] == "dismissed" and "migration planned" in dismissed["note"]

    assert api.post("/drift/spec-check/bamboohr", json={"spec_text": "openapi: 3.0.0\n"}).json() is None
    changed = api.post("/drift/spec-check/bamboohr", json={"spec_text": "openapi: 3.0.0\npaths: {}\n"}).json()
    assert changed["kind"] == "spec_changed"


def test_connection_calls_feed_the_monitor_and_repairs_verify_through_the_vault(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'oauth-drift.db'}", gateway_mode="mock", public_base_url="https://platform.example")
    with TestClient(create_app(settings)) as api:
        gusto = yaml.safe_load((ROOT / "manifests" / "gusto.yaml").read_text(encoding="utf-8"))
        assert api.post("/integrations/import", json={"manifest": gusto}).status_code == 201
        assert api.post("/integrations/gusto/versions/0.1.0/verify", json={"mode": "mock"}).json()["passed"]
        assert api.post("/integrations/gusto/versions/0.1.0/publish").status_code == 200
        tenant_id, conn_id = connect(api)

        inject(api, {"type": "rename_field", "endpoint_id": "list_employees", "old": "first_name", "new": "given_name"}, integration="gusto")
        result = api.post(f"/connections/{conn_id}/call", json={"endpoint_id": "list_employees", "params": {"company_uuid": "co-1"}})
        assert result.status_code == 200 and not result.json()["ok"]
        inc = api.get("/drift/incidents", params={"integration": "gusto"}).json()[0]
        assert inc["tenant_id"] == tenant_id

        # No connection given: the repair verifies through the tenant's vaulted tokens.
        outcome = api.post(f"/drift/incidents/{inc['id']}/repair", json={}).json()
        assert outcome["strategy"].startswith("deterministic:rename first_name->given_name")
        assert outcome["change_request"]["status"] == "pending" and outcome["report"]["passed"]
