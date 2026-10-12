"""carto_common.mtls: service roles in certificates and route checks (ADR 0031)."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography import x509

from carto_common.mtls import (
    ANY_ROLE,
    PEER_ROLE_STATE_KEY,
    ROLE_URI_PREFIX,
    ActAsRole,
    peer_role,
    role_allowed,
    role_from_peercert,
    role_uri,
)
from carto_common.pki import PkiError, create_ca, issue_certificate

TABLE = {
    ("POST", "/internal/ingest"): frozenset({"edge-gateway"}),
    ("GET", "/healthz"): frozenset({ANY_ROLE}),
}


def _peercert(*names: tuple[str, str]) -> dict[str, Any]:
    return {"subject": ((("commonName", "x"),),), "subjectAltName": names}


def test_role_uri_names_only_known_roles() -> None:
    assert role_uri("edge-gateway") == "urn:carto:role:edge-gateway"
    with pytest.raises(ValueError, match="unknown role"):
        role_uri("root")


@pytest.mark.parametrize(
    ("certificate", "expected"),
    [
        (_peercert(("DNS", "edge"), ("URI", "urn:carto:role:edge-gateway")), "edge-gateway"),
        (_peercert(("DNS", "edge")), None),
        (_peercert(("URI", "urn:carto:role:root")), None),
        (_peercert(("URI", "urn:carto:role:api"), ("URI", "urn:carto:role:proxy")), None),
        (_peercert(("DNS", "urn:carto:role:api")), None),  # a DNS name is not a role
        (_peercert(("URI", "https://urn:carto:role:api")), None),
        ({}, None),
        (None, None),
    ],
)
def test_role_from_peercert(certificate: dict[str, Any] | None, expected: str | None) -> None:
    assert role_from_peercert(certificate) == expected


@pytest.mark.parametrize(
    ("method", "route", "role", "allowed"),
    [
        ("POST", "/internal/ingest", "edge-gateway", True),
        ("POST", "/internal/ingest", "otel-collector", False),
        ("POST", "/internal/ingest", None, False),
        ("GET", "/internal/ingest", "edge-gateway", False),  # the method is part of the key
        ("GET", "/healthz", None, True),
        ("GET", "/healthz", "proxy", True),
        ("POST", "/unlisted", "edge-gateway", False),
        ("POST", None, "edge-gateway", False),
    ],
)
def test_role_allowed(method: str, route: str | None, role: str | None, allowed: bool) -> None:
    assert role_allowed(TABLE, method, route, role) is allowed


def test_peer_role_reads_only_known_roles_from_the_scope_state() -> None:
    assert peer_role({"state": {PEER_ROLE_STATE_KEY: "api"}}) == "api"
    assert peer_role({"state": {PEER_ROLE_STATE_KEY: "root"}}) is None
    assert peer_role({"state": {}}) is None
    assert peer_role({}) is None


async def test_act_as_role_records_the_role_without_touching_the_callers_scope() -> None:
    seen: list[str | None] = []

    async def app(scope: Any, _receive: Any, _send: Any) -> None:
        seen.append(peer_role(scope))

    wrapper = ActAsRole(app, "proxy")
    scope: dict[str, Any] = {"type": "http", "state": {}}
    await wrapper(scope, None, None)
    wrapper.role = None
    await wrapper(scope, None, None)
    assert seen == ["proxy", None]
    assert scope == {"type": "http", "state": {}}


def test_issued_certificates_carry_one_role_uri() -> None:
    ca = create_ca()
    leaf = issue_certificate(ca, "edge-gateway", role="edge-gateway")
    sans = leaf.certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    uris = sans.value.get_values_for_type(x509.UniformResourceIdentifier)
    assert uris == [ROLE_URI_PREFIX + "edge-gateway"]
    plain = issue_certificate(ca, "edge-gateway")
    sans = plain.certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert sans.value.get_values_for_type(x509.UniformResourceIdentifier) == []
    with pytest.raises(PkiError, match="unknown role"):
        issue_certificate(ca, "x", role="root")
