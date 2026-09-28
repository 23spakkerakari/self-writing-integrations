"""Mock-mode helpers, mounted only when GATEWAY_MODE=mock.

The MockEnvironment owns one mock authorization server per OAuth integration and builds the
composite transport (token endpoint plus API mock) that both the broker and the gateway use.
POST /mock/authorize plays the user's browser approving the consent screen at the provider."""
from __future__ import annotations

import time
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.manifest.schema import IntegrationManifest, OAuth2Auth
from app.oauth.broker import OAuthBroker
from app.registry.store import NotFound
from app.verification.mock_oauth import MockAuthorizationServer, composite_transport
from app.verification.mock_server import MockServer

router = APIRouter(prefix="/mock", tags=["mock"])


class MockEnvironment:
    def __init__(self, broker: OAuthBroker, clock: Callable[[], float] = time.time, access_ttl: int = 3600) -> None:
        self.broker = broker
        self.clock = clock
        self.access_ttl = access_ttl
        self.auth_servers: dict[str, MockAuthorizationServer] = {}

    def auth_server(self, manifest: IntegrationManifest) -> MockAuthorizationServer | None:
        if not isinstance(manifest.auth, OAuth2Auth):
            return None
        server = self.auth_servers.get(manifest.name)
        if server is None:
            try:
                client_id, client_secret, _ = self.broker.app_credentials(manifest.name)
            except NotFound:
                return None
            server = MockAuthorizationServer(manifest.auth.token_url, client_id, client_secret, access_ttl=self.access_ttl, clock=self.clock)
            self.auth_servers[manifest.name] = server
        return server

    def transport(self, manifest: IntegrationManifest) -> httpx.BaseTransport:
        server = self.auth_server(manifest)
        api = MockServer(manifest, bearer_validator=server.is_valid_access_token if server else None)
        routes = []
        if server is not None:
            routes.append((server.matches, server.handle))
        routes.append((lambda r: True, api.handle))
        return composite_transport(routes)


class AuthorizeRequest(BaseModel):
    authorize_url: str


class AuthorizeResponse(BaseModel):
    redirect_url: str
    state: str
    code: str


@router.post("/authorize", response_model=AuthorizeResponse)
def simulate_user_approval(body: AuthorizeRequest, request: Request) -> AuthorizeResponse:
    """Pretend the tenant clicked 'Allow' at the provider. Returns where the provider would redirect."""
    env: MockEnvironment = request.app.state.mock_env
    query = {k: v[0] for k, v in parse_qs(urlparse(body.authorize_url).query).items()}
    client_id = query.get("client_id")
    server = next((s for s in env.auth_servers.values() if s.client_id == client_id), None)
    if server is None:
        # The auth server is created lazily on first transport use; build it from the registered app.
        for integration in [c.integration_name for c in env.broker.list_connections()]:
            candidate = env.auth_server(env.broker.manifest_for(integration))
            if candidate is not None and candidate.client_id == client_id:
                server = candidate
                break
    if server is None:
        raise HTTPException(404, detail="no mock authorization server knows this client_id")
    try:
        redirect = server.authorize(body.authorize_url)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    q = {k: v[0] for k, v in parse_qs(urlparse(redirect).query).items()}
    return AuthorizeResponse(redirect_url=redirect, state=q["state"], code=q["code"])


@router.post("/revoke-at-provider/{integration_name}")
def revoke_at_provider(integration_name: str, request: Request) -> dict[str, str]:
    """Simulate the tenant revoking the app inside the provider's settings."""
    env: MockEnvironment = request.app.state.mock_env
    server = env.auth_servers.get(integration_name)
    if server is None:
        raise HTTPException(404, detail="no mock authorization server for this integration yet")
    server.revoke_all()
    return {"status": "revoked", "integration": integration_name}
