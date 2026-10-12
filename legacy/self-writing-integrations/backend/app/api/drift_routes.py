"""Drift control plane: incidents, repairs, change requests, canaries, approval policies,
promotion and rollback. This is the surface the developer UI (milestone 5) will sit on."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.common import ConnectionSpec
from app.drift.changes import ChangeError, ChangeRequests
from app.drift.models import (
    ApprovalPolicy,
    CanaryReport,
    ChangeRequest,
    ChangeStatus,
    DriftIncident,
    IncidentStatus,
    RiskClass,
    Triage,
)
from app.drift.monitor import DriftMonitor
from app.drift.repair import RepairError, RepairOutcome, RepairPipeline
from app.drift.worker import DriftWorker, WorkerTick
from app.manifest.schema import load_manifest
from app.registry.store import NotFound, Registry, RegistryError, VersionRecord
from app.runtime.gateway import ConfigError
from app.runtime.secrets import SecretNotFound
from app.synthesis.agent import SynthesisError

router = APIRouter(tags=["drift"])


# --- request models ------------------------------------------------------------------------


class Decision(BaseModel):
    actor: str = Field(default="human", description="Who is deciding; recorded on the change request")
    note: str = ""


class RepairRequest(BaseModel):
    connection_id: str | None = Field(default=None, description="OAuth connection whose credentials verify the repair")
    connection: ConnectionSpec | None = Field(default=None, description="Config and secrets for API-key integrations")


class CanaryStart(BaseModel):
    fraction: float | None = Field(default=None, gt=0, le=1, description="Defaults to CANARY_FRACTION")


class PromoteRequest(Decision):
    force: bool = Field(default=False, description="Promote even if the canary verdict is not 'pass'")


class PolicyUpdate(BaseModel):
    risk_class: RiskClass
    auto_approve: bool
    actor: str = "human"


class SpecCheck(BaseModel):
    spec_text: str


class TriageResponse(BaseModel):
    incident: DriftIncident
    triage: Triage


# --- dependencies -------------------------------------------------------------------------


def get_monitor(request: Request) -> DriftMonitor:
    return request.app.state.monitor


def get_changes(request: Request) -> ChangeRequests:
    return request.app.state.changes


def get_pipeline(request: Request) -> RepairPipeline:
    return request.app.state.pipeline


def get_worker(request: Request) -> DriftWorker:
    return request.app.state.worker


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


# --- incidents ------------------------------------------------------------------------------


@router.get("/drift/incidents", response_model=list[DriftIncident])
def list_incidents(
    integration: str | None = None,
    status: IncidentStatus | None = None,
    active: bool = Query(default=False, description="Only incidents that are not resolved or dismissed"),
    monitor: DriftMonitor = Depends(get_monitor),
) -> list[DriftIncident]:
    return monitor.list(integration=integration, status=status, active_only=active)


@router.get("/drift/incidents/{incident_id}", response_model=DriftIncident)
def get_incident(incident_id: int, monitor: DriftMonitor = Depends(get_monitor)) -> DriftIncident:
    try:
        return monitor.get(incident_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("/drift/incidents/{incident_id}/triage", response_model=TriageResponse)
def triage_incident(incident_id: int, pipeline: RepairPipeline = Depends(get_pipeline)) -> TriageResponse:
    try:
        incident, triage = pipeline.triage(incident_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return TriageResponse(incident=incident, triage=triage)


@router.post("/drift/incidents/{incident_id}/repair", response_model=RepairOutcome)
def repair_incident(
    incident_id: int,
    request: Request,
    body: RepairRequest | None = None,
    monitor: DriftMonitor = Depends(get_monitor),
    registry: Registry = Depends(get_registry),
    pipeline: RepairPipeline = Depends(get_pipeline),
) -> RepairOutcome:
    """Triage, build a candidate (mechanical patch or repair agent), verify it against the API as
    it behaves now, and open a change request. Nothing is published here."""
    body = body or RepairRequest()
    try:
        incident = monitor.get(incident_id)
        manifest = load_manifest(registry.get_published(incident.integration).manifest)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    try:
        world = request.app.state.world_for(manifest, connection_spec=body.connection, connection_id=body.connection_id, incident=incident)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    if world is None:
        raise HTTPException(400, detail="no connection to verify a repair against; pass connection_id or connection")
    try:
        return pipeline.run(incident_id, world)
    except RepairError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except (ConfigError, SecretNotFound) as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except SynthesisError as exc:
        raise HTTPException(502, detail=f"repair agent failed: {exc}") from exc


@router.post("/drift/incidents/{incident_id}/dismiss", response_model=DriftIncident)
def dismiss_incident(incident_id: int, body: Decision | None = None, monitor: DriftMonitor = Depends(get_monitor)) -> DriftIncident:
    body = body or Decision()
    try:
        return monitor.dismiss(incident_id, body.actor, body.note)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("/drift/spec-check/{name}", response_model=DriftIncident | None)
def spec_check(name: str, body: SpecCheck, monitor: DriftMonitor = Depends(get_monitor)) -> DriftIncident | None:
    """Scheduled spec re-fetch hands the current text here; a change opens a spec_changed incident."""
    return monitor.check_spec(name, body.spec_text)


@router.post("/drift/tick", response_model=WorkerTick)
def run_worker_tick(worker: DriftWorker = Depends(get_worker)) -> WorkerTick:
    """One pass of the drift worker, for operators and tests. The background loop runs the same code."""
    return worker.tick()


# --- change requests ----------------------------------------------------------------------------


@router.get("/changes", response_model=list[ChangeRequest])
def list_changes(
    integration: str | None = None, status: ChangeStatus | None = None, changes: ChangeRequests = Depends(get_changes)
) -> list[ChangeRequest]:
    return changes.list(integration=integration, status=status)


@router.get("/changes/{change_id}", response_model=ChangeRequest)
def get_change(change_id: int, changes: ChangeRequests = Depends(get_changes)) -> ChangeRequest:
    try:
        return changes.get(change_id)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("/changes/{change_id}/approve", response_model=ChangeRequest)
def approve_change(change_id: int, body: Decision | None = None, changes: ChangeRequests = Depends(get_changes)) -> ChangeRequest:
    body = body or Decision()
    return _change_op(lambda: changes.approve(change_id, body.actor, body.note))


@router.post("/changes/{change_id}/reject", response_model=ChangeRequest)
def reject_change(change_id: int, body: Decision | None = None, changes: ChangeRequests = Depends(get_changes)) -> ChangeRequest:
    body = body or Decision()
    return _change_op(lambda: changes.reject(change_id, body.actor, body.note))


@router.post("/changes/{change_id}/canary", response_model=ChangeRequest)
def start_canary(
    change_id: int, request: Request, body: CanaryStart | None = None, changes: ChangeRequests = Depends(get_changes)
) -> ChangeRequest:
    fraction = (body.fraction if body else None) or request.app.state.settings.canary_fraction
    return _change_op(lambda: changes.start_canary(change_id, fraction))


@router.get("/changes/{change_id}/canary", response_model=CanaryReport)
def canary_report(change_id: int, changes: ChangeRequests = Depends(get_changes)) -> CanaryReport:
    return _change_op(lambda: changes.canary_report(change_id))


@router.post("/changes/{change_id}/promote", response_model=ChangeRequest)
def promote_change(change_id: int, body: PromoteRequest | None = None, changes: ChangeRequests = Depends(get_changes)) -> ChangeRequest:
    body = body or PromoteRequest()
    return _change_op(lambda: changes.promote(change_id, body.actor, force=body.force))


@router.post("/changes/{change_id}/abort", response_model=ChangeRequest)
def abort_canary(change_id: int, body: Decision | None = None, changes: ChangeRequests = Depends(get_changes)) -> ChangeRequest:
    body = body or Decision()
    return _change_op(lambda: changes.abort_canary(change_id, body.actor, body.note))


# --- policy and rollback ------------------------------------------------------------------------


@router.get("/integrations/{name}/approval-policy", response_model=ApprovalPolicy)
def get_policy(name: str, changes: ChangeRequests = Depends(get_changes)) -> ApprovalPolicy:
    return changes.get_policy(name)


@router.put("/integrations/{name}/approval-policy", response_model=ApprovalPolicy)
def set_policy(name: str, body: PolicyUpdate, changes: ChangeRequests = Depends(get_changes)) -> ApprovalPolicy:
    return changes.set_policy(name, body.risk_class, body.auto_approve, actor=body.actor)


@router.post("/integrations/{name}/rollback", response_model=VersionRecord)
def rollback_integration(name: str, body: Decision | None = None, changes: ChangeRequests = Depends(get_changes)) -> VersionRecord:
    body = body or Decision()
    try:
        return changes.rollback(name, body.actor, body.note)
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except RegistryError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


def _change_op(fn):  # type: ignore[no-untyped-def]
    try:
        return fn()
    except NotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (ChangeError, RegistryError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
