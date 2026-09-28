"""HTTP control plane for milestone one: import or synthesize a manifest, verify it,
publish it, and call it through the gateway."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.api.mock_routes import MockEnvironment
from app.api.mock_routes import router as mock_router
from app.api.oauth_routes import router as oauth_router
from app.canonical.people import canonical_schemas
from app.config import Settings, load_settings
from app.db import Database, utcnow
from app.manifest.schema import IntegrationManifest, load_manifest
from app.oauth.broker import OAuthBroker
from app.oauth.scheduler import RefreshScheduler
from app.oauth.vault import Vault
from app.registry.store import IntegrationSummary, NotFound, Registry, RegistryError, VersionRecord
from app.runtime.gateway import CallResult, ConfigError, Connection, Gateway, GatewayError
from app.runtime.secrets import DictSecretsProvider, EnvSecretsProvider, SecretNotFound, SecretsProvider
from app.runtime.transforms import TRANSFORM_DOCS
from app.synthesis.pipeline import PipelineResult, synthesize_and_verify
from app.verification.harness import VerificationReport, verify

router = APIRouter()


# --- request / response models ---------------------------------------------------------


class ImportRequest(BaseModel):
    manifest: dict[str, Any]
    provenance: str = "manual"


class SynthesizeRequest(BaseModel):
    name: str = Field(description="Slug hint for the integration")
    spec_text: str = Field(description="OpenAPI YAML/JSON, docs text, or captured traffic")
    max_rounds: int = Field(default=2, ge=1, le=5)


class ConnectionSpec(BaseModel):
    tenant_id: str = "default"
    config: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(
        default_factory=dict, description="secret_ref -> environment variable name, resolved server-side"
    )
    secrets: dict[str, str] = Field(
        default_factory=dict, description="secret_ref -> value. Development only; use secret_env in real deployments"
    )


class VerifyRequest(BaseModel):
    mode: str = "mock"
    connection: ConnectionSpec = Field(default_factory=ConnectionSpec)


class CallRequest(BaseModel):
    endpoint_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    paginate: bool = True
    version: str | None = Field(default=None, description="Defaults to the published version")
    connection: ConnectionSpec = Field(default_factory=ConnectionSpec)


class _ChainSecrets:
    def __init__(self, *providers: SecretsProvider) -> None:
        self._providers = providers

    def get(self, ref: str) -> str:
        for p in self._providers:
            try:
                return p.get(ref)
            except SecretNotFound:
                continue
        raise SecretNotFound(f"secret '{ref}' not provided")


def _secrets(spec: ConnectionSpec) -> SecretsProvider:
    return _ChainSecrets(DictSecretsProvider(spec.secrets), EnvSecretsProvider(spec.secret_env))


# --- dependencies -------------------------------------------------------------------


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


# --- routes ----------------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/canonical")
def canonical() -> dict[str, Any]:
    return {"objects": canonical_schemas(), "transforms": TRANSFORM_DOCS}


@router.get("/manifest-schema")
def manifest_schema() -> dict[str, Any]:
    return IntegrationManifest.model_json_schema()


@router.get("/integrations", response_model=list[IntegrationSummary])
def list_integrations(registry: Registry = Depends(get_registry)) -> list[IntegrationSummary]:
    return registry.list_integrations()


@router.post("/integrations/import", response_model=VersionRecord, status_code=201)
def import_manifest(body: ImportRequest, registry: Registry = Depends(get_registry)) -> VersionRecord:
    try:
        manifest = load_manifest(body.manifest)
    except ValidationError as exc:
        raise HTTPException(422, detail=json.loads(exc.json())) from exc
    try:
        return registry.create_version(manifest, provenance=body.provenance)
    except RegistryError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.post("/integrations/synthesize", response_model=PipelineResult, status_code=201)
def synthesize_integration(
    body: SynthesizeRequest, registry: Registry = Depends(get_registry), settings: Settings = Depends(get_settings)
) -> PipelineResult:
    try:
        return synthesize_and_verify(
            registry,
            body.spec_text,
            body.name,
            model=settings.synthesis_model,
            max_attempts=settings.synthesis_max_attempts,
            max_rounds=body.max_rounds,
        )
    except RegistryError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:  # synthesis errors carry model output; surface them rather than a bare 500
        raise HTTPException(502, detail=f"synthesis failed: {exc}") from exc


@router.get("/integrations/{name}/versions", response_model=list[VersionRecord])
def list_versions(name: str, registry: Registry = Depends(get_registry)) -> list[VersionRecord]:
    try:
        return registry.list_versions(name)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.get("/integrations/{name}/versions/{version}", response_model=VersionRecord)
def get_version(name: str, version: str, registry: Registry = Depends(get_registry)) -> VersionRecord:
    try:
        return registry.get_version(name, version)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("/integrations/{name}/versions/{version}/verify", response_model=VerificationReport)
def verify_version(
    name: str,
    version: str,
    request: Request,
    body: VerifyRequest | None = None,
    registry: Registry = Depends(get_registry),
) -> VerificationReport:
    body = body or VerifyRequest()
    record = _record(registry, name, version)
    manifest = load_manifest(record.manifest)
    if body.mode == "mock":
        report = verify(manifest, mode="mock")
    elif body.mode == "live":
        transport = request.app.state.transport_factory(manifest)
        connection = Connection(tenant_id=body.connection.tenant_id, config=body.connection.config)
        try:
            report = verify(manifest, mode="live", connection=connection, secrets=_secrets(body.connection), transport=transport)
        except (ConfigError, SecretNotFound) as exc:
            raise HTTPException(400, detail=str(exc)) from exc
    else:
        raise HTTPException(422, detail="mode must be 'mock' or 'live'")
    registry.record_verification(name, version, report.model_dump(mode="json"), report.passed)
    return report


@router.post("/integrations/{name}/versions/{version}/publish", response_model=VersionRecord)
def publish_version(name: str, version: str, registry: Registry = Depends(get_registry)) -> VersionRecord:
    try:
        return registry.publish(name, version)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.post("/integrations/{name}/call", response_model=CallResult)
def call_integration(
    name: str, body: CallRequest, request: Request, registry: Registry = Depends(get_registry)
) -> CallResult:
    try:
        record = registry.get_version(name, body.version) if body.version else registry.get_published(name)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    manifest = load_manifest(record.manifest)
    transport = request.app.state.transport_factory(manifest)
    connection = Connection(tenant_id=body.connection.tenant_id, config=body.connection.config)
    try:
        with Gateway(manifest, connection, _secrets(body.connection), transport=transport) as gateway:
            return gateway.call(body.endpoint_id, body.params, paginate=body.paginate)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (ConfigError, SecretNotFound, GatewayError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc


def _record(registry: Registry, name: str, version: str) -> VersionRecord:
    try:
        return registry.get_version(name, version)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


# --- app factory ------------------------------------------------------------------------


def create_app(settings: Settings | None = None, clock: Callable[[], datetime] | None = None) -> FastAPI:
    """Wire the registry, vault, broker and transports. `clock` lets tests drive time."""
    settings = settings or load_settings()
    db = Database(settings.database_url)
    registry = Registry(db)
    vault = Vault.from_settings(db, settings.vault_master_key)
    clock = clock or utcnow

    mock_env: MockEnvironment | None = None

    def transport_factory(manifest: IntegrationManifest) -> httpx.BaseTransport | None:
        if mock_env is not None:
            return mock_env.transport(manifest)
        return None

    broker = OAuthBroker(db, vault, registry, transport_factory=transport_factory, clock=clock)
    if settings.gateway_mode == "mock":
        mock_env = MockEnvironment(broker, clock=lambda: clock().timestamp())

    scheduler = RefreshScheduler(broker, settings.refresh_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.refresh_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Self-writing integrations", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.registry = registry
    app.state.vault = vault
    app.state.broker = broker
    app.state.scheduler = scheduler
    app.state.transport_factory = transport_factory
    app.state.mock_env = mock_env
    app.include_router(router)
    app.include_router(oauth_router)
    if mock_env is not None:
        app.include_router(mock_router)
    return app
