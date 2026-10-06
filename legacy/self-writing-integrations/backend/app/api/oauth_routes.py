"""Tenancy and OAuth control plane: tenants, registered apps, connections, consent, callback,
refresh, revoke, audit, notifications, and calling an integration through a connection."""
from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.manifest.schema import load_manifest
from app.oauth.broker import ConsentError, ConsentRequest, OAuthBroker, OAuthError, ReconsentRequired
from app.oauth.models import AuthEvent, ConnectionRecord, Notification, OAuthApp, Tenant
from app.registry.store import NotFound
from app.runtime.gateway import CallResult, ConfigError, Connection, Gateway, GatewayError
from app.runtime.secrets import SecretNotFound

router = APIRouter()


def get_broker(request: Request) -> OAuthBroker:
    return request.app.state.broker


class TenantCreate(BaseModel):
    name: str


class AppRegister(BaseModel):
    integration_name: str
    client_id: str
    client_secret: str
    redirect_uri: str | None = Field(default=None, description="Defaults to <PUBLIC_BASE_URL>/oauth/callback")


class ConnectionCreate(BaseModel):
    tenant_id: str
    integration_name: str
    config: dict[str, Any] = Field(default_factory=dict)


class SecretsStore(BaseModel):
    secrets: dict[str, str] = Field(description="secret_ref -> value. Stored encrypted; never returned.")
    actor: str = "tenant"


class SecretsStatus(BaseModel):
    required: list[str]
    stored: list[str]


class ConsentStart(BaseModel):
    endpoint_ids: list[str] | None = Field(default=None, description="Limit scopes to these endpoints")


class ConnectionCall(BaseModel):
    endpoint_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    paginate: bool = True


@router.post("/tenants", response_model=Tenant, status_code=201)
def create_tenant(body: TenantCreate, broker: OAuthBroker = Depends(get_broker)) -> Tenant:
    return broker.create_tenant(body.name)


@router.get("/tenants", response_model=list[Tenant])
def list_tenants(broker: OAuthBroker = Depends(get_broker)) -> list[Tenant]:
    return broker.list_tenants()


@router.post("/oauth/apps", response_model=OAuthApp, status_code=201)
def register_app(body: AppRegister, request: Request, broker: OAuthBroker = Depends(get_broker)) -> OAuthApp:
    redirect_uri = body.redirect_uri or request.app.state.settings.public_base_url.rstrip("/") + "/oauth/callback"
    try:
        return broker.register_app(body.integration_name, body.client_id, body.client_secret, redirect_uri)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.post("/connections", response_model=ConnectionRecord, status_code=201)
def create_connection(body: ConnectionCreate, broker: OAuthBroker = Depends(get_broker)) -> ConnectionRecord:
    try:
        return broker.create_connection(body.tenant_id, body.integration_name, body.config)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get("/connections", response_model=list[ConnectionRecord])
def list_connections(tenant_id: str | None = None, broker: OAuthBroker = Depends(get_broker)) -> list[ConnectionRecord]:
    return broker.list_connections(tenant_id)


@router.get("/connections/{connection_id}", response_model=ConnectionRecord)
def get_connection(connection_id: str, broker: OAuthBroker = Depends(get_broker)) -> ConnectionRecord:
    try:
        return broker.get_connection(connection_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.get("/connections/{connection_id}/secrets", response_model=SecretsStatus)
def secrets_status(connection_id: str, broker: OAuthBroker = Depends(get_broker)) -> SecretsStatus:
    """Which static credentials the integration needs and which are in the vault. Names only."""
    try:
        conn = broker.get_connection(connection_id)
        return SecretsStatus(
            required=broker.manifest_for(conn.integration_name).secret_refs(), stored=broker.stored_secret_refs(connection_id)
        )
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("/connections/{connection_id}/secrets", response_model=SecretsStatus)
def store_secrets(connection_id: str, body: SecretsStore, broker: OAuthBroker = Depends(get_broker)) -> SecretsStatus:
    """Store an API key or token for a non-OAuth connection so flows and probes can run unattended."""
    try:
        stored = broker.store_secrets(connection_id, body.secrets, actor=body.actor)
        conn = broker.get_connection(connection_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return SecretsStatus(required=broker.manifest_for(conn.integration_name).secret_refs(), stored=stored)


@router.post("/connections/{connection_id}/consent", response_model=ConsentRequest)
def begin_consent(connection_id: str, body: ConsentStart | None = None, broker: OAuthBroker = Depends(get_broker)) -> ConsentRequest:
    body = body or ConsentStart()
    try:
        return broker.begin_consent(connection_id, body.endpoint_ids)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get("/connections/{connection_id}/consent-page", response_class=HTMLResponse)
def consent_page(connection_id: str, broker: OAuthBroker = Depends(get_broker)) -> str:
    """The human-in-the-loop screen: shows exactly which scopes will be requested and links to the provider."""
    try:
        consent = broker.begin_consent(connection_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    scopes = "".join(f"<li><code>{html.escape(s)}</code></li>" for s in consent.scopes) or "<li>(no scopes)</li>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Authorize {html.escape(consent.integration_name)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a}}
a.button{{display:inline-block;padding:.6rem 1.2rem;background:#1f5eff;color:#fff;border-radius:.4rem;text-decoration:none}}</style></head>
<body><h1>Connect {html.escape(consent.integration_name)}</h1>
<p>The platform will request the following permissions on your behalf. Nothing is requested beyond what the
integration's endpoints need.</p><ul>{scopes}</ul>
<p>This request expires at {consent.expires_at.isoformat()}.</p>
<p><a class="button" href="{html.escape(consent.authorize_url)}">Continue to {html.escape(consent.integration_name)}</a></p>
<p><small>Connection {html.escape(connection_id)} · state {html.escape(consent.state[:8])}…</small></p></body></html>"""


@router.get("/oauth/callback")
def oauth_callback(
    state: str = Query(...),
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    broker: OAuthBroker = Depends(get_broker),
) -> ConnectionRecord:
    if error:
        raise HTTPException(400, detail=f"provider returned {error}: {error_description or ''}".strip())
    if not code:
        raise HTTPException(400, detail="missing code")
    try:
        return broker.complete_consent(state, code)
    except ConsentError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(502, detail=str(exc)) from exc


@router.post("/connections/{connection_id}/refresh", response_model=ConnectionRecord)
def force_refresh(connection_id: str, broker: OAuthBroker = Depends(get_broker)) -> ConnectionRecord:
    try:
        broker.refresh(connection_id, reason="manual")
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ReconsentRequired as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    return broker.get_connection(connection_id)


@router.post("/connections/{connection_id}/revoke", response_model=ConnectionRecord)
def revoke(connection_id: str, broker: OAuthBroker = Depends(get_broker)) -> ConnectionRecord:
    try:
        return broker.revoke(connection_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.get("/connections/{connection_id}/audit", response_model=list[AuthEvent])
def connection_audit(connection_id: str, broker: OAuthBroker = Depends(get_broker)) -> list[AuthEvent]:
    return broker.audit(connection_id=connection_id)


@router.get("/audit", response_model=list[AuthEvent])
def tenant_audit(tenant_id: str, broker: OAuthBroker = Depends(get_broker)) -> list[AuthEvent]:
    """Every auth event for a tenant across all of its connections, oldest first."""
    return broker.audit(tenant_id=tenant_id)


@router.get("/notifications", response_model=list[Notification])
def notifications(tenant_id: str, unread_only: bool = False, broker: OAuthBroker = Depends(get_broker)) -> list[Notification]:
    return broker.notifications(tenant_id, unread_only)


@router.post("/notifications/{notification_id}/read", status_code=204, response_model=None)
def mark_notification_read(notification_id: int, broker: OAuthBroker = Depends(get_broker)) -> Response:
    broker.mark_read(notification_id)
    return Response(status_code=204)


@router.post("/connections/{connection_id}/call", response_model=CallResult)
def call_through_connection(connection_id: str, body: ConnectionCall, request: Request, broker: OAuthBroker = Depends(get_broker)) -> CallResult:
    """The multi-tenant call path: credentials come from the vault via the broker, never from the caller."""
    try:
        conn = broker.get_connection(connection_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    if conn.status != "active":
        raise HTTPException(409, detail=f"connection is {conn.status}")
    changes = request.app.state.changes
    try:
        record, canary, arm = changes.select_version(conn.integration_name)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    manifest = load_manifest(record.manifest)
    transport = request.app.state.transport_factory(manifest)
    try:
        with Gateway(manifest, Connection(tenant_id=conn.tenant_id, config=conn.config), broker.secrets_for(conn.id), transport=transport) as gateway:
            result = gateway.call(body.endpoint_id, body.params, paginate=body.paginate)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (ConfigError, SecretNotFound, GatewayError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if canary is not None:
        changes.record_call(canary.id, arm, record.version, result)
    if arm == "base":
        request.app.state.monitor.ingest_result(result, tenant_id=conn.tenant_id)
    return result
