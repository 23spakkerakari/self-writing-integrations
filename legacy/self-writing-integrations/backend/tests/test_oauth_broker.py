"""Broker lifecycle against the mock authorization server with a controllable clock."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.db import Database
from app.manifest.schema import load_manifest_file
from app.oauth.broker import ConsentError, OAuthBroker, ReconsentRequired
from app.oauth.scheduler import RefreshScheduler
from app.oauth.vault import Vault
from app.registry.store import Registry
from app.runtime.gateway import Connection, Gateway
from app.verification.mock_oauth import MockAuthorizationServer, composite_transport
from app.verification.mock_server import MockServer
from tests.conftest import ROOT

GUSTO = ROOT / "manifests" / "gusto.yaml"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def timestamp(self) -> float:
        return self.now.timestamp()

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class World:
    """Registry with gusto published, a vault, a mock provider, and a broker wired to it."""

    def __init__(self, tmp_path, access_ttl: int = 3600) -> None:
        self.clock = FakeClock()
        self.db = Database(f"sqlite:///{tmp_path / 'broker.db'}")
        self.registry = Registry(self.db)
        self.manifest = load_manifest_file(str(GUSTO))
        self.registry.create_version(self.manifest)
        self.registry.record_verification("gusto", "0.1.0", {}, passed=True)
        self.registry.publish("gusto", "0.1.0")
        self.vault = Vault(self.db, os.urandom(32))
        self.provider = MockAuthorizationServer(
            self.manifest.auth.token_url, "client-123", "shh", access_ttl=access_ttl, clock=self.clock.timestamp
        )
        self.api = MockServer(self.manifest, bearer_validator=self.provider.is_valid_access_token)
        self.transport = composite_transport([(self.provider.matches, self.provider.handle), (lambda r: True, self.api.handle)])
        self.notified = []
        self.broker = OAuthBroker(
            self.db, self.vault, self.registry, transport_factory=lambda m: self.transport, clock=self.clock, on_notify=self.notified.append
        )
        self.broker.register_app("gusto", "client-123", "shh", "https://platform.example/oauth/callback")
        self.tenant = self.broker.create_tenant("Acme")

    def connect(self):
        conn = self.broker.create_connection(self.tenant.id, "gusto", {})
        consent = self.broker.begin_consent(conn.id)
        redirect = self.provider.authorize(consent.authorize_url)
        q = {k: v[0] for k, v in parse_qs(urlparse(redirect).query).items()}
        return self.broker.complete_consent(q["state"], q["code"])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_consent_requests_minimal_scopes_with_pkce(world):
    conn = world.broker.create_connection(world.tenant.id, "gusto", {})
    assert conn.status == "pending_consent"
    consent = world.broker.begin_consent(conn.id)
    q = {k: v[0] for k, v in parse_qs(urlparse(consent.authorize_url).query).items()}
    assert q["client_id"] == "client-123"
    assert q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://platform.example/oauth/callback"
    assert q["scope"].split(" ") == ["companies:read", "employees:read"]
    assert consent.scopes == ["companies:read", "employees:read"]
    # Limiting to one endpoint narrows the scope set.
    narrowed = world.broker.begin_consent(conn.id, endpoint_ids=[])
    assert narrowed.scopes == ["companies:read"]


def test_consent_completes_and_tokens_land_in_vault(world):
    conn = world.connect()
    assert conn.status == "active"
    assert conn.granted_scopes == ["companies:read", "employees:read"]
    assert conn.token_expires_at == world.clock.now + timedelta(seconds=3600)
    access, _ = world.vault.get_credential(world.tenant.id, conn.id, "access_token")
    assert world.provider.is_valid_access_token(access)
    assert world.vault.get_credential(world.tenant.id, conn.id, "refresh_token") is not None
    events = [e.event for e in world.broker.audit(connection_id=conn.id)]
    assert events == ["connection_created", "consent_started", "consent_granted"]
    # The state cannot be replayed.
    with pytest.raises(ConsentError):
        world.broker.complete_consent("nope", "code")


def test_consent_state_expires(world):
    conn = world.broker.create_connection(world.tenant.id, "gusto", {})
    consent = world.broker.begin_consent(conn.id)
    redirect = world.provider.authorize(consent.authorize_url)
    q = {k: v[0] for k, v in parse_qs(urlparse(redirect).query).items()}
    world.clock.advance(minutes=11)
    with pytest.raises(ConsentError, match="expired"):
        world.broker.complete_consent(q["state"], q["code"])
    assert world.broker.audit(connection_id=conn.id)[-1].event == "consent_failed"


def test_pkce_mismatch_is_rejected(world):
    conn = world.broker.create_connection(world.tenant.id, "gusto", {})
    consent = world.broker.begin_consent(conn.id)
    redirect = world.provider.authorize(consent.authorize_url)
    q = {k: v[0] for k, v in parse_qs(urlparse(redirect).query).items()}
    # Start a second consent: the stored verifier changes, so the old code's challenge no longer matches.
    world.broker.begin_consent(conn.id)
    with pytest.raises(ConsentError):
        world.broker.complete_consent(q["state"], q["code"])


def test_gateway_calls_api_with_broker_token(world):
    conn = world.connect()
    with Gateway(world.manifest, Connection(config={}), world.broker.secrets_for(conn.id), transport=world.transport, sleep=lambda s: None) as gw:
        result = gw.call("list_employees", {"company_uuid": "co-1"})
    assert result.ok, result.drift_events
    # The mock alternates the `terminated` boolean, and the enum_map turns it into a status.
    assert [c["employment_status"] for c in result.canonical] == ["terminated", "active", "terminated"]
    assert world.api.requests[0].headers["Authorization"].startswith("Bearer at-")


def test_gateway_refreshes_after_401_and_retries(world):
    conn = world.connect()
    world.provider.expire_access_tokens()  # provider-side expiry the platform could not predict
    with Gateway(world.manifest, Connection(config={}), world.broker.secrets_for(conn.id), transport=world.transport, sleep=lambda s: None) as gw:
        result = gw.call("get_employee", {"employee_uuid": "e-1"})
    assert result.ok, result.drift_events
    statuses = [r.url.path for r in world.api.requests]
    assert len(statuses) == 2  # 401 then success
    assert world.provider.refresh_count == 1
    assert world.broker.get_connection(conn.id).refresh_count == 1


def test_ensure_fresh_refreshes_inside_leeway(world):
    conn = world.connect()
    first = world.broker.ensure_fresh(conn.id)
    world.clock.advance(seconds=3000)  # 600s left, leeway is 300s -> no refresh yet
    assert world.broker.ensure_fresh(conn.id) == first
    world.clock.advance(seconds=400)  # 200s left -> refresh
    second = world.broker.ensure_fresh(conn.id)
    assert second != first
    assert world.provider.refresh_count == 1


def test_a_week_of_autonomous_refreshes(world):
    conn = world.connect()
    scheduler = RefreshScheduler(world.broker)
    refreshed = 0
    for hour in range(24 * 7):
        world.clock.advance(hours=1)
        tick = scheduler.tick()
        refreshed += len(tick.refreshed)
        assert not tick.reconsent and not tick.errors, tick
        # The token in the vault is always valid at the provider after a tick.
        access, _ = world.vault.get_credential(world.tenant.id, conn.id, "access_token")
        assert world.provider.is_valid_access_token(access)
    record = world.broker.get_connection(conn.id)
    assert record.status == "active"
    assert refreshed == 24 * 7 == record.refresh_count
    assert world.notified == []
    assert all(e.event == "token_refreshed" for e in world.broker.audit(connection_id=conn.id)[3:])


def test_revocation_at_provider_flips_to_needs_reconsent_and_notifies(world):
    conn = world.connect()
    world.provider.revoke_all()
    world.clock.advance(hours=1)
    tick = RefreshScheduler(world.broker).tick()
    assert tick.reconsent == [conn.id]
    record = world.broker.get_connection(conn.id)
    assert record.status == "needs_reconsent"
    assert world.vault.get_credential(world.tenant.id, conn.id, "access_token") is None
    assert len(world.notified) == 1 and world.notified[0].kind == "reconsent_required"
    assert world.broker.notifications(world.tenant.id, unread_only=True)[0].connection_id == conn.id
    with pytest.raises(ReconsentRequired):
        world.broker.ensure_fresh(conn.id)
    events = [e.event for e in world.broker.audit(connection_id=conn.id)]
    assert events == ["connection_created", "consent_started", "consent_granted", "refresh_failed", "reconsent_required"]
    # A second tick does not spam: the connection is no longer active.
    assert RefreshScheduler(world.broker).tick().checked == 0


def test_reconsent_restores_the_connection(world):
    conn = world.connect()
    world.provider.revoke_all()
    world.clock.advance(hours=1)
    RefreshScheduler(world.broker).tick()
    world.provider.revoked = False  # the user re-installs the app at the provider
    consent = world.broker.begin_consent(conn.id)
    redirect = world.provider.authorize(consent.authorize_url)
    q = {k: v[0] for k, v in parse_qs(urlparse(redirect).query).items()}
    restored = world.broker.complete_consent(q["state"], q["code"])
    assert restored.status == "active"
    assert world.broker.ensure_fresh(conn.id).startswith("at-")


def test_revoke_destroys_credentials(world):
    conn = world.connect()
    record = world.broker.revoke(conn.id)
    assert record.status == "revoked"
    assert world.vault.get_credential(world.tenant.id, conn.id, "access_token") is None
    assert world.broker.audit(connection_id=conn.id)[-1].event == "revoked"
    assert world.broker.due_for_refresh() == []


def test_audit_log_reconstructs_history_per_tenant(world):
    conn = world.connect()
    other_tenant = world.broker.create_tenant("Globex")
    other = world.broker.create_connection(other_tenant.id, "gusto", {})
    mine = world.broker.audit(tenant_id=world.tenant.id)
    assert {e.connection_id for e in mine} == {conn.id}
    theirs = world.broker.audit(tenant_id=other_tenant.id)
    assert [e.event for e in theirs] == ["connection_created"] and theirs[0].connection_id == other.id
    assert mine[2].scopes == ["companies:read", "employees:read"] and mine[2].actor == "tenant"


def test_client_secret_basic_method(tmp_path):
    world = World(tmp_path)
    data = world.manifest.model_dump(mode="json")
    data["auth"]["token_auth_method"] = "client_secret_basic"
    data["version"] = "0.1.1"
    m = load_manifest_file.__globals__["load_manifest"](data)
    world.registry.create_version(m)
    world.registry.record_verification("gusto", "0.1.1", {}, passed=True)
    world.registry.publish("gusto", "0.1.1")
    conn = world.connect()
    assert conn.status == "active"
    assert "client_secret" not in world.provider.token_requests[-1]


def test_non_oauth_connection_is_active_immediately(tmp_path):
    world = World(tmp_path)
    bamboo = load_manifest_file(str(ROOT / "manifests" / "bamboohr.yaml"))
    world.registry.create_version(bamboo)
    conn = world.broker.create_connection(world.tenant.id, "bamboohr", {"company_domain": "acme"})
    assert conn.status == "active"
    with pytest.raises(Exception, match="missing"):
        world.broker.create_connection(world.tenant.id, "bamboohr", {})
