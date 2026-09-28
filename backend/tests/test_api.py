def test_health_and_reference_endpoints(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["mode"] == "mock"
    canonical = client.get("/canonical").json()
    assert "Employee" in canonical["objects"] and "to_date" in canonical["transforms"]
    assert client.get("/manifest-schema").json()["title"] == "IntegrationManifest"


def test_import_verify_publish_call_flow(client, manifest_dict):
    created = client.post("/integrations/import", json={"manifest": manifest_dict})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"

    # Cannot publish an unverified draft.
    assert client.post("/integrations/bamboohr/versions/0.1.0/publish").status_code == 409

    report = client.post("/integrations/bamboohr/versions/0.1.0/verify", json={"mode": "mock"})
    assert report.status_code == 200, report.text
    assert report.json()["passed"] is True

    published = client.post("/integrations/bamboohr/versions/0.1.0/publish")
    assert published.status_code == 200 and published.json()["status"] == "published"

    listing = client.get("/integrations").json()
    assert listing[0]["published_version"] == "0.1.0"

    call = client.post(
        "/integrations/bamboohr/call",
        json={
            "endpoint_id": "get_employee",
            "params": {"id": "1000"},
            "connection": {"config": {"company_domain": "acme"}, "secrets": {"api_key": "dev-key"}},
        },
    )
    assert call.status_code == 200, call.text
    body = call.json()
    assert body["ok"] is True
    assert body["canonical"][0]["source_id"] == "1000"
    assert body["canonical"][0]["employment_status"] == "active"


def test_call_requires_published_version(client, manifest_dict):
    client.post("/integrations/import", json={"manifest": manifest_dict})
    response = client.post("/integrations/bamboohr/call", json={"endpoint_id": "list_employees"})
    assert response.status_code == 404


def test_call_reports_missing_config(client, manifest_dict):
    client.post("/integrations/import", json={"manifest": manifest_dict})
    client.post("/integrations/bamboohr/versions/0.1.0/verify", json={"mode": "mock"})
    client.post("/integrations/bamboohr/versions/0.1.0/publish")
    response = client.post(
        "/integrations/bamboohr/call",
        json={"endpoint_id": "list_employees", "connection": {"secrets": {"api_key": "k"}}},
    )
    assert response.status_code == 400
    assert "company_domain" in response.json()["detail"]


def test_invalid_manifest_import_is_422(client, manifest_dict):
    bad = dict(manifest_dict, endpoints=[])
    response = client.post("/integrations/import", json={"manifest": bad})
    assert response.status_code == 422


def test_duplicate_import_is_409(client, manifest_dict):
    assert client.post("/integrations/import", json={"manifest": manifest_dict}).status_code == 201
    assert client.post("/integrations/import", json={"manifest": manifest_dict}).status_code == 409


def test_live_verify_uses_mock_transport_in_mock_gateway_mode(client, manifest_dict):
    client.post("/integrations/import", json={"manifest": manifest_dict})
    response = client.post(
        "/integrations/bamboohr/versions/0.1.0/verify",
        json={"mode": "live", "connection": {"config": {"company_domain": "acme"}, "secrets": {"api_key": "k"}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "live" and response.json()["passed"] is True
