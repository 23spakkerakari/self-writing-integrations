"""The HTTP flow in mock gateway mode: register app, connect, consent, callback, call, revoke."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.routes import create_app
from app.config import Settings
from tests.conftest import ROOT


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def api(tmp_path, clock):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'oauth-api.db'}", gateway_mode="mock", public_base_url="https://platform.example")
    app = create_app(settings, clock=clock)
    with TestClient(app) as client:
        gusto = yaml.safe_load((ROOT / "manifests" / "gusto.yaml").read_text(encoding="utf-8"))
        assert client.post("/integrations/import", json={"manifest": gusto}).status_code == 201
        assert client.post("/integrations/gusto/versions/0.1.0/verify", json={"mode": "mock"}).json()["passed"]
        assert client.post("/integrations/gusto/versions/0.1.0/publish").status_code == 200
        yield client


def connect(api: TestClient) -> tuple[str, str]:
    tenant = api.post("/tenants", json={"name": "Acme"}).json()
    app = api.post("/oauth/apps", json={"integration_name": "gusto", "client_id": "cid", "client_secret": "csecret"})
    assert app.status_code == 201, app.text
    assert app.json()["redirect_uri"] == "https://platform.example/oauth/callback"
    assert "client_secret" not in app.json()
    conn = api.post("/connections", json={"tenant_id": tenant["id"], "integration_name": "gusto"}).json()
    assert conn["status"] == "pending_consent"
    consent = api.post(f"/connections/{conn['id']}/consent").json()
    assert consent["scopes"] == ["companies:read", "employees:read"]
    approval = api.post("/mock/authorize", json={"authorize_url": consent["authorize_url"]})
    assert approval.status_code == 200, approval.text
    callback = api.get("/oauth/callback", params={"state": approval.json()["state"], "code": approval.json()["code"]})
    assert callback.status_code == 200, callback.text
    assert callback.json()["status"] == "active"
    return tenant["id"], conn["id"]


def test_full_consent_flow_and_call(api):
    tenant_id, conn_id = connect(api)
    call = api.post(f"/connections/{conn_id}/call", json={"endpoint_id": "list_employees", "params": {"company_uuid": "co-1"}})
    assert call.status_code == 200, call.text
    assert call.json()["ok"] is True
    assert call.json()["canonical"][0]["source_integration"] == "gusto"
    audit = api.get(f"/connections/{conn_id}/audit").json()
    assert [e["event"] for e in audit] == ["connection_created", "consent_started", "consent_granted"]
    assert api.get("/notifications", params={"tenant_id": tenant_id}).json() == []


def test_consent_page_lists_scopes(api):
    tenant = api.post("/tenants", json={"name": "Acme"}).json()
    api.post("/oauth/apps", json={"integration_name": "gusto", "client_id": "cid", "client_secret": "csecret"})
    conn = api.post("/connections", json={"tenant_id": tenant["id"], "integration_name": "gusto"}).json()
    page = api.get(f"/connections/{conn['id']}/consent-page")
    assert page.status_code == 200
    assert "employees:read" in page.text and "Continue to gusto" in page.text


def test_callback_rejects_bad_state_and_provider_errors(api):
    connect(api)
    assert api.get("/oauth/callback", params={"state": "bogus", "code": "x"}).status_code == 400
    assert api.get("/oauth/callback", params={"state": "bogus", "error": "access_denied"}).status_code == 400


def test_manual_refresh_and_revocation_at_provider(api, clock):
    tenant_id, conn_id = connect(api)
    before = api.get(f"/connections/{conn_id}").json()["token_expires_at"]
    clock.now += timedelta(minutes=30)
    refreshed = api.post(f"/connections/{conn_id}/refresh")
    assert refreshed.status_code == 200 and refreshed.json()["refresh_count"] == 1
    assert refreshed.json()["token_expires_at"] > before

    assert api.post("/mock/revoke-at-provider/gusto").status_code == 200
    failed = api.post(f"/connections/{conn_id}/refresh")
    assert failed.status_code == 409
    assert api.get(f"/connections/{conn_id}").json()["status"] == "needs_reconsent"
    notes = api.get("/notifications", params={"tenant_id": tenant_id, "unread_only": "true"}).json()
    assert len(notes) == 1 and notes[0]["kind"] == "reconsent_required"
    assert api.post(f"/connections/{conn_id}/call", json={"endpoint_id": "get_employee", "params": {"employee_uuid": "e"}}).status_code == 409


def test_revoke_endpoint(api):
    _, conn_id = connect(api)
    assert api.post(f"/connections/{conn_id}/revoke").json()["status"] == "revoked"
    assert api.get("/connections").json()[0]["status"] == "revoked"


def test_app_registration_requires_oauth_manifest(api, manifest_dict):
    api.post("/integrations/import", json={"manifest": manifest_dict})
    response = api.post("/oauth/apps", json={"integration_name": "bamboohr", "client_id": "a", "client_secret": "b"})
    assert response.status_code == 400
    assert api.post("/oauth/apps", json={"integration_name": "nope", "client_id": "a", "client_secret": "b"}).status_code == 404


def test_tenant_listing_audit_feed_and_notification_read(api):
    assert api.get("/tenants").json() == []
    tenant_id, conn_id = connect(api)
    tenants = api.get("/tenants").json()
    assert [t["id"] for t in tenants] == [tenant_id] and tenants[0]["name"] == "Acme"

    feed = api.get("/audit", params={"tenant_id": tenant_id}).json()
    assert [e["event"] for e in feed] == ["connection_created", "consent_started", "consent_granted"]
    assert all(e["connection_id"] == conn_id for e in feed)
    assert api.get("/audit", params={"tenant_id": "nobody"}).json() == []

    api.post("/mock/revoke-at-provider/gusto")
    assert api.post(f"/connections/{conn_id}/refresh").status_code == 409
    notes = api.get("/notifications", params={"tenant_id": tenant_id}).json()
    assert len(notes) == 1 and notes[0]["read"] is False
    assert api.post(f"/notifications/{notes[0]['id']}/read").status_code == 204
    assert api.get("/notifications", params={"tenant_id": tenant_id, "unread_only": "true"}).json() == []


def test_reconsent_after_provider_revocation_restores_calls(api):
    tenant_id, conn_id = connect(api)
    api.post("/mock/revoke-at-provider/gusto")
    assert api.post(f"/connections/{conn_id}/refresh").status_code == 409
    assert api.get(f"/connections/{conn_id}").json()["status"] == "needs_reconsent"

    consent = api.post(f"/connections/{conn_id}/consent").json()
    approval = api.post("/mock/authorize", json={"authorize_url": consent["authorize_url"]}).json()
    restored = api.get("/oauth/callback", params={"state": approval["state"], "code": approval["code"]}).json()
    assert restored["status"] == "active"

    call = api.post(f"/connections/{conn_id}/call", json={"endpoint_id": "list_employees", "params": {"company_uuid": "co-1"}})
    assert call.status_code == 200 and call.json()["ok"] is True, call.text
    assert api.post(f"/connections/{conn_id}/refresh").status_code == 200
    assert api.get(f"/connections/{conn_id}").json()["status"] == "active"
