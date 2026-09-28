"""Verification harness: exercise every endpoint of a manifest and grade the result.

Mock mode runs against the spec-derived mock server and proves the manifest is internally
consistent: auth is sent, paths resolve, pagination terminates, responses validate, and
mappings yield usable canonical objects. Live mode runs the same checks against the real API
with a real connection and is the gate for publishing once credentials exist.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field

from app.canonical.people import CANONICAL_OBJECTS, Employee
from app.manifest.schema import Endpoint, IntegrationManifest
from app.runtime.gateway import Connection, Gateway, GatewayError
from app.runtime.secrets import DictSecretsProvider, SecretsProvider
from app.verification.mock_server import MockServer

Mode = Literal["mock", "live"]


class EndpointCheck(BaseModel):
    endpoint_id: str
    passed: bool
    status_code: int | None = None
    pages: int = 0
    records: int = 0
    canonical: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    integration: str
    version: str
    mode: Mode
    passed: bool
    checks: list[EndpointCheck]
    started_at: datetime
    finished_at: datetime

    def summary(self) -> str:
        lines = [f"{self.integration}@{self.version} [{self.mode}] {'PASS' if self.passed else 'FAIL'}"]
        for c in self.checks:
            state = "ok " if c.passed else "FAIL"
            lines.append(f"  {state} {c.endpoint_id}: status={c.status_code} pages={c.pages} records={c.records} canonical={c.canonical}")
            lines.extend(f"       - {e}" for e in c.errors)
            lines.extend(f"       ~ {w}" for w in c.warnings)
        return "\n".join(lines)


def verify(
    manifest: IntegrationManifest,
    mode: Mode = "mock",
    connection: Connection | None = None,
    secrets: SecretsProvider | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    mock_hook: Any = None,
) -> VerificationReport:
    started = datetime.now(timezone.utc)
    if mode == "mock":
        connection = Connection(tenant_id="mock", config={v: f"mock-{v}" for v in manifest.config_vars})
        secrets = DictSecretsProvider({ref: f"mock-{ref}" for ref in manifest.secret_refs()})
        transport = MockServer(manifest, hook=mock_hook).transport()
        sleep = lambda s: None  # noqa: E731 - never wait on a mock
    elif connection is None or secrets is None:
        raise ValueError("live verification needs a connection and a secrets provider")

    checks: list[EndpointCheck] = []
    known_ids: list[str] = []
    with Gateway(manifest, connection, secrets, transport=transport, sleep=sleep) as gateway:
        # List-style endpoints first so their ids can feed the path parameters of detail endpoints.
        ordered = sorted(manifest.endpoints, key=lambda e: (e.path.count("{"), e.items_path is None))
        for endpoint in ordered:
            checks.append(_check_endpoint(gateway, manifest, endpoint, known_ids, mode))
    finished = datetime.now(timezone.utc)
    return VerificationReport(
        integration=manifest.name,
        version=manifest.version,
        mode=mode,
        passed=all(c.passed for c in checks),
        checks=checks,
        started_at=started,
        finished_at=finished,
    )


def _sample_params(endpoint: Endpoint, known_ids: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for p in endpoint.parameters:
        if p.location == "path" or (p.required and p.name not in endpoint.default_query):
            looks_like_id = "id" in p.name.lower()
            params[p.name] = known_ids[0] if (looks_like_id and known_ids) else "1"
    return params


def _check_endpoint(
    gateway: Gateway, manifest: IntegrationManifest, endpoint: Endpoint, known_ids: list[str], mode: Mode
) -> EndpointCheck:
    check = EndpointCheck(endpoint_id=endpoint.id, passed=True)
    try:
        result = gateway.call(endpoint.id, _sample_params(endpoint, known_ids), max_pages=5)
    except GatewayError as exc:
        check.passed = False
        check.errors.append(str(exc))
        return check

    check.status_code = result.status_code
    check.pages = result.pages
    check.records = len(result.records)
    check.canonical = len(result.canonical)
    check.errors.extend(f"{e.kind}: {e.detail}" for e in result.drift_events)
    check.errors.extend(f"mapping: {m}" for m in result.mapping_errors)
    if result.status_code is None or not (200 <= result.status_code < 300):
        check.passed = False

    mapping = manifest.mapping_for(endpoint.id)
    if mapping is not None:
        if result.records and not result.canonical:
            check.errors.append("mapping produced no canonical objects from non-empty records")
        for c in result.canonical:
            known_ids.append(c["source_id"])
        if mapping.canonical_object == "Employee":
            for i, c in enumerate(result.canonical):
                if not any(c.get(f) for f in Employee.IDENTITY_FIELDS):
                    check.errors.append(f"canonical[{i}] has no identity field ({', '.join(Employee.IDENTITY_FIELDS)})")
        if not result.records:
            check.warnings.append("endpoint returned no records" + ("" if mode == "live" else " from the mock"))
    elif endpoint.items_path is not None or endpoint.response_schema is not None:
        check.warnings.append("no mapping declared; records are passthrough only")

    if check.errors:
        check.passed = False
    return check


__all__ = ["verify", "VerificationReport", "EndpointCheck", "CANONICAL_OBJECTS"]
