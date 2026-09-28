import httpx

from app.verification.harness import verify
from app.verification.mock_server import MockServer, SchemaExampleGenerator


def test_reference_manifest_passes_mock_verification(manifest):
    report = verify(manifest, mode="mock")
    assert report.passed, report.summary()
    by_id = {c.endpoint_id: c for c in report.checks}
    assert by_id["list_employees"].records == 3
    assert by_id["list_employees"].canonical == 3
    assert by_id["get_employee"].canonical == 1
    # The detail endpoint was called with an id harvested from the list endpoint.
    assert by_id["get_employee"].status_code == 200


def test_list_endpoints_run_before_detail_endpoints(manifest):
    report = verify(manifest, mode="mock")
    assert [c.endpoint_id for c in report.checks] == ["list_employees", "get_employee"]


def test_renamed_field_fails_verification(manifest):
    def hook(endpoint_id, request, body):
        if endpoint_id == "list_employees":
            for emp in body["employees"]:
                emp["display_name"] = emp.pop("displayName")
        return body

    report = verify(manifest, mode="mock", mock_hook=hook)
    assert not report.passed
    failed = [c for c in report.checks if not c.passed]
    assert [c.endpoint_id for c in failed] == ["list_employees"]
    assert any("schema_violation" in e for e in failed[0].errors)
    assert "FAIL list_employees" in report.summary()


def test_auth_failure_fails_verification(manifest):
    def hook(endpoint_id, request, body):
        return httpx.Response(401, json={"error": "bad key"})

    report = verify(manifest, mode="mock", mock_hook=hook)
    assert not report.passed
    assert all("unexpected_status: HTTP 401" in e for c in report.checks for e in c.errors)


def test_mock_server_rejects_unauthenticated_requests(manifest):
    mock = MockServer(manifest)
    with httpx.Client(transport=mock.transport()) as client:
        response = client.get("https://api.bamboohr.com/api/gateway.php/acme/v1/employees/directory")
    assert response.status_code == 401


def test_mock_server_prefers_literal_routes(manifest):
    mock = MockServer(manifest)
    with httpx.Client(transport=mock.transport(), auth=("k", "x")) as client:
        directory = client.get("https://x/v1/employees/directory").json()
        one = client.get("https://x/v1/employees/17").json()
    assert "employees" in directory
    assert "employees" not in one and "id" in one


def test_example_generator_respects_schema_hints():
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "workEmail": {"type": ["string", "null"], "format": "email"},
            "status": {"type": "string", "enum": ["Active", "Inactive"]},
            "count": {"type": "integer", "minimum": 5},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            "nested": {"$ref": "#/$defs/Nested"},
        },
        "$defs": {"Nested": {"type": "object", "properties": {"hireDate": {"type": "string", "format": "date"}}}},
    }
    value = SchemaExampleGenerator(schema).generate()
    assert value["id"] == "1000"
    assert "@example.com" in value["workEmail"]
    assert value["status"] == "Active"
    assert value["count"] >= 5
    assert len(value["tags"]) == 2
    assert value["nested"]["hireDate"].startswith("2020-")
