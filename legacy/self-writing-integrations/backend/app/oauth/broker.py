"""The OAuth broker.

Consent is the only human step: the tenant is shown the requested scopes and approves at the
provider. Everything after that is autonomous: code exchange, storage in the vault, refresh ahead
of expiry, and detection of revoked grants (which flips the connection to needs_reconsent and
notifies the tenant). Every credential event is appended to the audit log.

Connections to integrations that use a static credential (API key, bearer token, basic auth)
keep that credential in the same vault, so unattended work such as flows and scheduled probes
never needs a caller to supply it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import Database, as_utc, utcnow
from app.manifest.schema import IntegrationManifest, OAuth2Auth, load_manifest
from app.oauth.models import (
    AuthEvent,
    AuthEventRow,
    ConnectionRecord,
    ConnectionRow,
    Notification,
    NotificationRow,
    OAuthApp,
    OAuthAppRow,
    Tenant,
    TenantRow,
)
from app.oauth.vault import Vault
from app.registry.store import NotFound, Registry
from app.runtime.secrets import SecretNotFound

log = logging.getLogger(__name__)

CONSENT_TTL = timedelta(minutes=10)
_SECRET_KIND = "secret:"  # vault credential kind prefix for static secrets, e.g. 'secret:api_key'
TransportFactory = Callable[[IntegrationManifest], httpx.BaseTransport | None]
NotifyHook = Callable[["Notification"], None]


class OAuthError(Exception):
    pass


class ConsentError(OAuthError):
    pass


class ReconsentRequired(OAuthError):
    pass


class ConsentRequest(BaseModel):
    connection_id: str
    integration_name: str
    authorize_url: str
    state: str
    scopes: list[str]
    expires_at: datetime


class TokenSet(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scope: str | None = None


class BrokerSecrets:
    """SecretsProvider for a connection. On an OAuth connection, resolving 'access_token' refreshes
    it if it is about to expire and refresh() is what the gateway calls after a 401. On any other
    connection the secret refs of the manifest resolve to the values stored in the vault."""

    def __init__(self, broker: "OAuthBroker", connection_id: str) -> None:
        self._broker = broker
        self._connection_id = connection_id

    def get(self, ref: str) -> str:
        try:
            return self._broker.resolve_secret(self._connection_id, ref)
        except OAuthError as exc:
            raise SecretNotFound(str(exc)) from exc

    def refresh(self) -> bool:
        try:
            self._broker.refresh(self._connection_id, reason="401 from API")
            return True
        except OAuthError:
            return False


class OAuthBroker:
    def __init__(
        self,
        db: Database,
        vault: Vault,
        registry: Registry,
        transport_factory: TransportFactory | None = None,
        clock: Callable[[], datetime] = utcnow,
        on_notify: NotifyHook | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.db = db
        self.vault = vault
        self.registry = registry
        self._transport_factory = transport_factory
        self.clock = clock
        self._on_notify = on_notify
        self._timeout = timeout
        self._manifest_cache: dict[tuple[str, str], IntegrationManifest] = {}

    # --- tenants --------------------------------------------------------------------------

    def create_tenant(self, name: str) -> Tenant:
        with self.db.session() as s:
            row = TenantRow(name=name)
            s.add(row)
            s.commit()
            return Tenant(id=row.id, name=row.name, created_at=as_utc(row.created_at) or utcnow())

    def list_tenants(self) -> list[Tenant]:
        with self.db.session() as s:
            rows = s.scalars(select(TenantRow).order_by(TenantRow.created_at))
            return [Tenant(id=r.id, name=r.name, created_at=as_utc(r.created_at) or utcnow()) for r in rows]

    def get_tenant(self, tenant_id: str) -> Tenant:
        with self.db.session() as s:
            row = s.get(TenantRow, tenant_id)
            if row is None:
                raise NotFound(f"unknown tenant '{tenant_id}'")
            return Tenant(id=row.id, name=row.name, created_at=as_utc(row.created_at) or utcnow())

    # --- OAuth apps ---------------------------------------------------------------------------

    def register_app(self, integration_name: str, client_id: str, client_secret: str, redirect_uri: str) -> OAuthApp:
        manifest = self.manifest_for(integration_name)
        if not isinstance(manifest.auth, OAuth2Auth):
            raise OAuthError(f"integration '{integration_name}' does not use oauth2 auth")
        with self.db.session() as s:
            row = s.get(OAuthAppRow, integration_name)
            if row is None:
                row = OAuthAppRow(integration_name=integration_name, client_id=client_id, redirect_uri=redirect_uri, client_secret_ciphertext=b"")
                s.add(row)
            row.client_id = client_id
            row.redirect_uri = redirect_uri
            row.client_secret_ciphertext = self.vault.encrypt_platform(f"oauth-app:{integration_name}", client_secret)
            s.commit()
            s.refresh(row)
            return OAuthApp(integration_name=row.integration_name, client_id=row.client_id, redirect_uri=row.redirect_uri, created_at=as_utc(row.created_at) or utcnow())

    def get_app(self, integration_name: str) -> OAuthApp:
        row = self._app_row(integration_name)
        return OAuthApp(integration_name=row.integration_name, client_id=row.client_id, redirect_uri=row.redirect_uri, created_at=as_utc(row.created_at) or utcnow())

    def app_credentials(self, integration_name: str) -> tuple[str, str, str]:
        """(client_id, client_secret, redirect_uri). Only the broker and the mock environment need this."""
        row = self._app_row(integration_name)
        return row.client_id, self.vault.decrypt_platform(f"oauth-app:{integration_name}", row.client_secret_ciphertext), row.redirect_uri

    def _app_row(self, integration_name: str) -> OAuthAppRow:
        with self.db.session() as s:
            row = s.get(OAuthAppRow, integration_name)
            if row is None:
                raise NotFound(f"no OAuth app registered for '{integration_name}'")
            return row

    # --- connections ---------------------------------------------------------------------------

    def create_connection(self, tenant_id: str, integration_name: str, config: dict[str, Any] | None = None) -> ConnectionRecord:
        self.get_tenant(tenant_id)
        manifest = self.manifest_for(integration_name)
        config = dict(config or {})
        missing = [v for v in manifest.config_vars if v not in config]
        if missing:
            raise OAuthError(f"connection config is missing {missing} required by '{integration_name}'")
        status = "pending_consent" if isinstance(manifest.auth, OAuth2Auth) else "active"
        with self.db.session() as s:
            row = ConnectionRow(tenant_id=tenant_id, integration_name=integration_name, config_json=json.dumps(config), status=status)
            s.add(row)
            s.commit()
            s.refresh(row)
            record = ConnectionRecord.from_row(row)
        self._audit(tenant_id, record.id, "connection_created", detail=f"integration={integration_name} status={status}")
        return record

    def get_connection(self, connection_id: str) -> ConnectionRecord:
        with self.db.session() as s:
            return ConnectionRecord.from_row(self._conn_row(s, connection_id))

    def list_connections(self, tenant_id: str | None = None) -> list[ConnectionRecord]:
        with self.db.session() as s:
            stmt = select(ConnectionRow).order_by(ConnectionRow.created_at)
            if tenant_id:
                stmt = stmt.where(ConnectionRow.tenant_id == tenant_id)
            return [ConnectionRecord.from_row(r) for r in s.scalars(stmt)]

    # --- consent (human in the loop) ---------------------------------------------------------

    def begin_consent(self, connection_id: str, endpoint_ids: list[str] | None = None, actor: str = "tenant") -> ConsentRequest:
        conn = self.get_connection(connection_id)
        manifest = self.manifest_for(conn.integration_name)
        auth = manifest.auth
        if not isinstance(auth, OAuth2Auth):
            raise OAuthError("this connection does not use OAuth")
        app = self.get_app(conn.integration_name)
        scopes = manifest.required_scopes(endpoint_ids)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)[:96]
        expires_at = self.clock() + CONSENT_TTL
        params = {
            "response_type": "code",
            "client_id": app.client_id,
            "redirect_uri": app.redirect_uri,
            "scope": auth.scope_separator.join(scopes),
            "state": state,
            **auth.extra_authorization_params,
        }
        if auth.pkce:
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            params.update(code_challenge=challenge, code_challenge_method="S256")
        with self.db.session() as s:
            row = self._conn_row(s, connection_id)
            row.pending_state = state
            row.pending_verifier = verifier if auth.pkce else None
            row.pending_scopes = " ".join(scopes)
            row.pending_expires_at = expires_at
            s.commit()
        self._audit(conn.tenant_id, connection_id, "consent_started", scopes=scopes, actor=actor)
        sep = "&" if "?" in auth.authorization_url else "?"
        return ConsentRequest(
            connection_id=connection_id,
            integration_name=conn.integration_name,
            authorize_url=auth.authorization_url + sep + urlencode(params),
            state=state,
            scopes=scopes,
            expires_at=expires_at,
        )

    def complete_consent(self, state: str, code: str, actor: str = "tenant") -> ConnectionRecord:
        with self.db.session() as s:
            row = s.scalar(select(ConnectionRow).where(ConnectionRow.pending_state == state))
            if row is None:
                raise ConsentError("unknown or already used state")
            expires = as_utc(row.pending_expires_at)
            if expires is None or expires < self.clock():
                row.pending_state = None
                s.commit()
                self._audit(row.tenant_id, row.id, "consent_failed", detail="state expired", actor=actor)
                raise ConsentError("consent request expired; start again")
            connection_id, tenant_id, integration = row.id, row.tenant_id, row.integration_name
            verifier, requested = row.pending_verifier, row.pending_scopes

        manifest = self.manifest_for(integration)
        auth = manifest.auth
        assert isinstance(auth, OAuth2Auth)
        client_id, client_secret, redirect_uri = self.app_credentials(integration)
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if verifier:
            data["code_verifier"] = verifier
        try:
            tokens = self._token_request(manifest, auth, data, client_id, client_secret)
        except OAuthError as exc:
            self._audit(tenant_id, connection_id, "consent_failed", detail=str(exc), actor=actor)
            raise
        granted = (tokens.scope or requested).split(" ") if (tokens.scope or requested) else []
        self._store_tokens(tenant_id, connection_id, tokens)
        with self.db.session() as s:
            row = self._conn_row(s, connection_id)
            row.status = "active"
            row.granted_scopes = " ".join(granted)
            row.pending_state = row.pending_verifier = None
            row.pending_scopes = ""
            row.pending_expires_at = None
            row.token_expires_at = tokens.expires_at
            s.commit()
            s.refresh(row)
            record = ConnectionRecord.from_row(row)
        self._audit(tenant_id, connection_id, "consent_granted", scopes=granted, actor=actor)
        return record

    # --- tokens (autonomous) ---------------------------------------------------------------

    def ensure_fresh(self, connection_id: str, force: bool = False) -> str:
        """Return a usable access token, refreshing first when it is inside the leeway window."""
        conn = self.get_connection(connection_id)
        if conn.status != "active":
            raise ReconsentRequired(f"connection is {conn.status}; consent is required") if conn.status != "revoked" else OAuthError("connection is revoked")
        manifest = self.manifest_for(conn.integration_name)
        auth = manifest.auth
        if not isinstance(auth, OAuth2Auth):
            raise OAuthError("not an OAuth connection")
        stored = self.vault.get_credential(conn.tenant_id, connection_id, "access_token")
        if stored is None:
            raise ReconsentRequired("no access token stored; consent is required")
        token, expires_at = stored
        leeway = timedelta(seconds=auth.refresh_leeway_seconds)
        if force or (expires_at is not None and expires_at - leeway <= self.clock()):
            return self.refresh(connection_id, reason="forced" if force else "expiring").access_token
        return token

    def refresh(self, connection_id: str, reason: str = "scheduled") -> TokenSet:
        conn = self.get_connection(connection_id)
        if conn.status != "active":
            raise ReconsentRequired(f"connection is {conn.status}")
        manifest = self.manifest_for(conn.integration_name)
        auth = manifest.auth
        assert isinstance(auth, OAuth2Auth)
        stored = self.vault.get_credential(conn.tenant_id, connection_id, "refresh_token")
        if stored is None:
            self._mark_reconsent(conn, "provider issued no refresh token")
            raise ReconsentRequired("no refresh token; consent is required")
        refresh_token, _ = stored
        client_id, client_secret, _ = self.app_credentials(conn.integration_name)
        try:
            tokens = self._token_request(manifest, auth, {"grant_type": "refresh_token", "refresh_token": refresh_token}, client_id, client_secret)
        except InvalidGrant as exc:
            self._audit(conn.tenant_id, connection_id, "refresh_failed", detail=f"{reason}: {exc}")
            self._mark_reconsent(conn, str(exc))
            raise ReconsentRequired(str(exc)) from exc
        except OAuthError as exc:
            # Transient provider failure: keep the connection active, report, let the scheduler retry.
            self._audit(conn.tenant_id, connection_id, "refresh_failed", detail=f"{reason}: {exc}")
            raise
        if tokens.refresh_token is None:
            tokens.refresh_token = refresh_token
        self._store_tokens(conn.tenant_id, connection_id, tokens)
        with self.db.session() as s:
            row = self._conn_row(s, connection_id)
            row.token_expires_at = tokens.expires_at
            row.last_refreshed_at = self.clock()
            row.refresh_count += 1
            s.commit()
        self._audit(conn.tenant_id, connection_id, "token_refreshed", detail=reason)
        return tokens

    def due_for_refresh(self, now: datetime | None = None) -> list[ConnectionRecord]:
        now = now or self.clock()
        due: list[ConnectionRecord] = []
        for conn in self.list_connections():
            if conn.status != "active" or conn.token_expires_at is None:
                continue
            manifest = self.manifest_for(conn.integration_name)
            if not isinstance(manifest.auth, OAuth2Auth):
                continue
            if conn.token_expires_at - timedelta(seconds=manifest.auth.refresh_leeway_seconds) <= now:
                due.append(conn)
        return due

    def revoke(self, connection_id: str, actor: str = "tenant") -> ConnectionRecord:
        conn = self.get_connection(connection_id)
        removed = self.vault.delete_credentials(connection_id)
        with self.db.session() as s:
            row = self._conn_row(s, connection_id)
            row.status = "revoked"
            row.token_expires_at = None
            s.commit()
            s.refresh(row)
            record = ConnectionRecord.from_row(row)
        self._audit(conn.tenant_id, connection_id, "revoked", actor=actor, detail=f"{removed} credential(s) destroyed")
        return record

    def secrets_for(self, connection_id: str) -> BrokerSecrets:
        return BrokerSecrets(self, connection_id)

    # --- static credentials -----------------------------------------------------------------

    def store_secrets(self, connection_id: str, secrets_by_ref: dict[str, str], actor: str = "tenant") -> list[str]:
        """Put the static credentials of a non-OAuth connection in the vault. Values are write-only:
        nothing returns them, and the audit log records which refs were stored, never the values."""
        conn = self.get_connection(connection_id)
        manifest = self.manifest_for(conn.integration_name)
        if isinstance(manifest.auth, OAuth2Auth):
            raise OAuthError("OAuth connections receive their tokens through consent; there is nothing to store")
        if conn.status == "revoked":
            raise OAuthError("connection is revoked")
        allowed = manifest.secret_refs()
        unknown = sorted(set(secrets_by_ref) - set(allowed))
        if unknown:
            raise OAuthError(f"'{conn.integration_name}' has no secret refs {unknown}; expected {allowed}")
        empty = sorted(ref for ref, value in secrets_by_ref.items() if not value)
        if empty:
            raise OAuthError(f"secret values for {empty} are empty")
        for ref, value in secrets_by_ref.items():
            self.vault.put_credential(conn.tenant_id, connection_id, _SECRET_KIND + ref, value)
        self._audit(conn.tenant_id, connection_id, "secrets_stored", actor=actor, detail="refs=" + ",".join(sorted(secrets_by_ref)))
        return self.stored_secret_refs(connection_id)

    def stored_secret_refs(self, connection_id: str) -> list[str]:
        self.get_connection(connection_id)
        kinds = self.vault.credential_kinds(connection_id)
        return sorted(kind[len(_SECRET_KIND) :] for kind in kinds if kind.startswith(_SECRET_KIND))

    def resolve_secret(self, connection_id: str, ref: str) -> str:
        conn = self.get_connection(connection_id)
        manifest = self.manifest_for(conn.integration_name)
        if isinstance(manifest.auth, OAuth2Auth):
            if ref != "access_token":
                raise SecretNotFound(f"OAuth connections only expose 'access_token', not '{ref}'")
            return self.ensure_fresh(connection_id)
        if conn.status != "active":
            raise SecretNotFound(f"connection is {conn.status}")
        stored = self.vault.get_credential(conn.tenant_id, connection_id, _SECRET_KIND + ref)
        if stored is None:
            raise SecretNotFound(f"connection {connection_id[:8]} has no stored secret '{ref}'; store it before running unattended work")
        return stored[0]

    def notify(self, tenant_id: str, connection_id: str | None, kind: str, message: str) -> None:
        """Tenant-facing notification for platform events outside the auth lifecycle (flows)."""
        self._notify(tenant_id, connection_id, kind, message)

    # --- audit and notifications ----------------------------------------------------------

    def audit(self, connection_id: str | None = None, tenant_id: str | None = None) -> list[AuthEvent]:
        with self.db.session() as s:
            stmt = select(AuthEventRow).order_by(AuthEventRow.id)
            if connection_id:
                stmt = stmt.where(AuthEventRow.connection_id == connection_id)
            if tenant_id:
                stmt = stmt.where(AuthEventRow.tenant_id == tenant_id)
            return [AuthEvent.from_row(r) for r in s.scalars(stmt)]

    def notifications(self, tenant_id: str, unread_only: bool = False) -> list[Notification]:
        with self.db.session() as s:
            stmt = select(NotificationRow).where(NotificationRow.tenant_id == tenant_id).order_by(NotificationRow.id)
            if unread_only:
                stmt = stmt.where(NotificationRow.read.is_(False))
            return [Notification.from_row(r) for r in s.scalars(stmt)]

    def mark_read(self, notification_id: int) -> None:
        with self.db.session() as s:
            row = s.get(NotificationRow, notification_id)
            if row is not None:
                row.read = True
                s.commit()

    # --- internals ----------------------------------------------------------------------------

    def manifest_for(self, integration_name: str) -> IntegrationManifest:
        try:
            record = self.registry.get_published(integration_name)
        except NotFound:
            versions = self.registry.list_versions(integration_name)
            record = versions[-1]
        key = (record.name, record.version)
        manifest = self._manifest_cache.get(key)
        if manifest is None:
            manifest = load_manifest(record.manifest)
            self._manifest_cache[key] = manifest
        return manifest

    def _token_request(self, manifest: IntegrationManifest, auth: OAuth2Auth, data: dict[str, str], client_id: str, client_secret: str) -> TokenSet:
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        if auth.token_auth_method == "client_secret_basic":
            headers["Authorization"] = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        else:
            data = {**data, "client_id": client_id, "client_secret": client_secret}
        transport = self._transport_factory(manifest) if self._transport_factory else None
        try:
            with httpx.Client(transport=transport, timeout=self._timeout) as client:
                response = client.post(auth.token_url, data=data, headers=headers)
        except httpx.TransportError as exc:
            raise OAuthError(f"token endpoint unreachable: {exc}") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            error = body.get("error", f"HTTP {response.status_code}")
            description = body.get("error_description", response.text[:200])
            if error == "invalid_grant":
                raise InvalidGrant(f"{error}: {description}")
            raise OAuthError(f"token endpoint returned {error}: {description}")
        body = response.json()
        if "access_token" not in body:
            raise OAuthError("token response has no access_token")
        expires_in = body.get("expires_in")
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=(self.clock() + timedelta(seconds=int(expires_in))) if expires_in else None,
            scope=body.get("scope"),
        )

    def _store_tokens(self, tenant_id: str, connection_id: str, tokens: TokenSet) -> None:
        self.vault.put_credential(tenant_id, connection_id, "access_token", tokens.access_token, tokens.expires_at)
        if tokens.refresh_token:
            self.vault.put_credential(tenant_id, connection_id, "refresh_token", tokens.refresh_token, None)

    def _mark_reconsent(self, conn: ConnectionRecord, why: str) -> None:
        self.vault.delete_credentials(conn.id)
        with self.db.session() as s:
            row = self._conn_row(s, conn.id)
            row.status = "needs_reconsent"
            row.token_expires_at = None
            s.commit()
        self._audit(conn.tenant_id, conn.id, "reconsent_required", detail=why)
        self._notify(conn.tenant_id, conn.id, "reconsent_required", f"Connection to {conn.integration_name} needs to be re-authorized: {why}")

    def _audit(self, tenant_id: str, connection_id: str | None, event: str, scopes: list[str] | None = None, actor: str = "system", detail: str = "") -> None:
        with self.db.session() as s:
            s.add(AuthEventRow(tenant_id=tenant_id, connection_id=connection_id, event=event, scopes=" ".join(scopes or []), actor=actor, detail=detail, at=self.clock()))
            s.commit()

    def _notify(self, tenant_id: str, connection_id: str | None, kind: str, message: str) -> None:
        with self.db.session() as s:
            row = NotificationRow(tenant_id=tenant_id, connection_id=connection_id, kind=kind, message=message, created_at=self.clock())
            s.add(row)
            s.commit()
            s.refresh(row)
            notification = Notification.from_row(row)
        log.warning("notification for tenant %s: %s", tenant_id, message)
        if self._on_notify is not None:
            self._on_notify(notification)

    @staticmethod
    def _conn_row(s: Any, connection_id: str) -> ConnectionRow:
        row = s.get(ConnectionRow, connection_id)
        if row is None:
            raise NotFound(f"unknown connection '{connection_id}'")
        return row


class InvalidGrant(OAuthError):
    """The provider says the grant is gone: revoked, expired, or reassigned."""


__all__ = ["OAuthBroker", "OAuthError", "ConsentError", "ReconsentRequired", "InvalidGrant", "ConsentRequest", "TokenSet", "BrokerSecrets"]
