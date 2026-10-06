import base64
import copy

import httpx
import pytest

from app.manifest.schema import load_manifest
from app.runtime.gateway import ConfigError, Connection, Gateway, GatewayError
from app.runtime.secrets import DictSecretsProvider
from app.verification.mock_server import MockServer


def make_gateway(manifest, hook=None, sleep=lambda s: None, **kwargs):
    mock = MockServer(manifest, hook=hook, **kwargs)
    gateway = Gateway(
        manifest,
        Connection(config={"company_domain": "acme"}),
        DictSecretsProvider({"api_key": "k123"}),
        transport=mock.transport(),
        sleep=sleep,
    )
    return gateway, mock


def test_basic_auth_and_base_url_templating(manifest):
    gateway, mock = make_gateway(manifest)
    with gateway:
        result = gateway.call("list_employees")
    assert result.ok, result.drift_events
    request = mock.requests[0]
    assert request.url.host == "api.bamboohr.com"
    assert "/gateway.php/acme/v1/employees/directory" in str(request.url)
    expected = "Basic " + base64.b64encode(b"k123:x").decode()
    assert request.headers["Authorization"] == expected
    assert request.headers["Accept"] == "application/json"


def test_records_are_extracted_and_mapped(manifest):
    gateway, _ = make_gateway(manifest, list_size=4)
    with gateway:
        result = gateway.call("list_employees")
    assert len(result.records) == 4
    assert result.canonical_object == "Employee"
    assert len(result.canonical) == 4
    first = result.canonical[0]
    assert first["source_id"] == "1000"
    assert first["work_email"].endswith("@example.com")
    assert first["employment_status"] == "active"


def test_path_and_query_params(manifest):
    gateway, mock = make_gateway(manifest)
    with gateway:
        result = gateway.call("get_employee", {"id": "42"})
    assert result.ok
    assert mock.requests[0].url.path.endswith("/employees/42")
    assert "fields=" in str(mock.requests[0].url)
    assert result.canonical[0]["hire_date"].startswith("2020-")


def test_missing_config_var_is_config_error(manifest):
    gateway = Gateway(manifest, Connection(config={}), DictSecretsProvider({"api_key": "k"}), transport=MockServer(manifest).transport())
    with pytest.raises(ConfigError, match="company_domain"):
        gateway.call("list_employees")


def test_undeclared_param_rejected(manifest):
    gateway, _ = make_gateway(manifest)
    with pytest.raises(GatewayError, match="undeclared"):
        gateway.call("list_employees", {"page": 2})


def test_retries_on_429_then_succeeds(manifest):
    attempts = {"n": 0}

    def hook(endpoint_id, request, body):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "slow down"})
        return body

    slept: list[float] = []
    gateway, _ = make_gateway(manifest, hook=hook, sleep=slept.append)
    with gateway:
        result = gateway.call("list_employees")
    assert result.ok
    assert attempts["n"] == 2
    assert slept and slept[0] >= 1.0


def test_gives_up_after_max_attempts(manifest):
    def hook(endpoint_id, request, body):
        return httpx.Response(503, json={"error": "down"})

    gateway, mock = make_gateway(manifest, hook=hook)
    with gateway:
        result = gateway.call("list_employees")
    assert not result.ok
    assert result.status_code == 503
    assert len(mock.requests) == manifest.retry.max_attempts
    assert result.drift_events[-1].kind == "unexpected_status"


def test_schema_violation_becomes_drift_event(manifest):
    def hook(endpoint_id, request, body):
        # The vendor renamed a field: displayName -> display_name.
        for emp in body["employees"]:
            emp["display_name"] = emp.pop("displayName")
        return body

    gateway, _ = make_gateway(manifest, hook=hook)
    with gateway:
        result = gateway.call("list_employees")
    assert not result.ok
    kinds = {e.kind for e in result.drift_events}
    assert kinds == {"schema_violation"}
    assert any("displayName" in e.detail for e in result.drift_events)
    # Records are still extracted so downstream can inspect them.
    assert len(result.records) == 3


def test_malformed_body_is_drift(manifest):
    def hook(endpoint_id, request, body):
        return httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "text/html"})

    gateway, _ = make_gateway(manifest, hook=hook)
    with gateway:
        result = gateway.call("list_employees")
    assert not result.ok
    assert result.drift_events[0].kind == "malformed_body"


def _paginated_manifest(manifest_dict, style):
    data = copy.deepcopy(manifest_dict)
    ep = data["endpoints"][0]
    ep["pagination"] = {"style": style, "page_size": 3, "size_param": "limit", "next_cursor_path": "next"}
    ep["response_schema"]["properties"]["next"] = {"type": ["string", "null"]}
    return load_manifest(data)


def test_page_pagination_stops_on_short_page(manifest_dict):
    m = _paginated_manifest(manifest_dict, "page")
    gateway, mock = make_gateway(m, list_size=3)
    with gateway:
        result = gateway.call("list_employees")
    assert result.ok
    assert result.pages == 2
    assert len(result.records) == 3
    assert [r.url.params.get("page") for r in mock.requests] == ["1", "2"]


def test_cursor_pagination_follows_next_cursor(manifest_dict):
    m = _paginated_manifest(manifest_dict, "cursor")
    gateway, mock = make_gateway(m, list_size=3)
    with gateway:
        result = gateway.call("list_employees")
    assert result.ok
    assert result.pages == 2
    assert mock.requests[1].url.params["cursor"] == "mock-cursor-2"


def test_paginate_false_fetches_one_page(manifest_dict):
    m = _paginated_manifest(manifest_dict, "page")
    gateway, _ = make_gateway(m, list_size=3)
    with gateway:
        result = gateway.call("list_employees", paginate=False)
    assert result.pages == 1


def test_rate_limiter_sleeps_when_burst_exhausted(manifest_dict):
    data = copy.deepcopy(manifest_dict)
    data["rate_limit"] = {"requests_per_second": 1, "burst": 1}
    m = load_manifest(data)
    slept: list[float] = []
    gateway, _ = make_gateway(m, sleep=slept.append)
    with gateway:
        gateway.call("list_employees")
        gateway.call("list_employees")
    assert slept and slept[0] > 0
