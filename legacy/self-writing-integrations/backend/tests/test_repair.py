"""The repair pipeline end to end: detect -> triage -> candidate -> verify against the drifted
world -> change request -> approval -> canary -> promote, plus rollback, policies, failure paths
and the worker. The repair agent is scripted so the suite runs offline; the last test calls
Claude for real and is skipped without credentials."""
from __future__ import annotations

import copy
from typing import Any, Callable

import pytest

from app.drift.changes import ChangeError
from app.drift.repair import RepairPipeline
from app.drift.worker import DriftWorker
from app.manifest.schema import load_manifest
from app.synthesis.agent import SynthesisResult
from app.verification.drift_scenarios import (
    NonJson,
    PathMoved,
    RenameField,
    RequireHeader,
    RetypeField,
    SetField,
    StatusResponse,
    Sunset,
    WrapItems,
)
from tests.conftest import DriftEnv, has_anthropic_credentials

Fix = Callable[[dict[str, Any]], dict[str, Any]]


# --- scripted agents: what a correct repair looks like for each scenario ------------------------


def fix_enum(data: dict[str, Any]) -> dict[str, Any]:
    endpoint = next(e for e in data["endpoints"] if e["id"] == "get_employee")
    endpoint["response_schema"]["properties"]["status"]["enum"].append("OnLeave")
    mapping = next(m for m in data["mappings"] if m["endpoint_id"] == "get_employee")
    field = next(f for f in mapping["fields"] if f["target"] == "employment_status")
    field["args"]["map"]["OnLeave"] = "on_leave"
    return data


def fix_auth(data: dict[str, Any]) -> dict[str, Any]:
    data["auth"] = {"type": "api_key", "location": "header", "name": "X-Api-Key", "secret_ref": "api_key"}
    return data


def fix_path(data: dict[str, Any]) -> dict[str, Any]:
    next(e for e in data["endpoints"] if e["id"] == "list_employees")["path"] = "/employees/directory/v2"
    return data


def fix_retype(data: dict[str, Any]) -> dict[str, Any]:
    endpoint = next(e for e in data["endpoints"] if e["id"] == "list_employees")
    endpoint["response_schema"]["properties"]["employees"]["items"]["properties"]["id"] = {"type": ["string", "integer"]}
    return data


def wrong_fix(data: dict[str, Any]) -> dict[str, Any]:
    """Plausible but wrong: renames the schema field to a name the API does not use."""
    endpoint = next(e for e in data["endpoints"] if e["id"] == "get_employee")
    endpoint["response_schema"]["properties"]["status"]["enum"].append("On Leave")
    return data


def unchanged(data: dict[str, Any]) -> dict[str, Any]:
    return data


def scripted(*fixes: Fix):
    """A stand-in for the repair agent that applies the next fix in the list per call."""
    queue = list(fixes)
    calls: list[str] = []

    def repair_fn(spec_text, name_hint, model, max_attempts, client, previous, feedback):
        calls.append(feedback)
        fix = queue.pop(0) if len(queue) > 1 else queue[0]
        return SynthesisResult(manifest=load_manifest(fix(copy.deepcopy(previous.model_dump(mode="json")))), attempts=1, transcript=[feedback])

    repair_fn.calls = calls  # type: ignore[attr-defined]
    return repair_fn


def never_called(*args, **kwargs):
    raise AssertionError("the repair agent should not have been called")


def detect(env: DriftEnv, world, endpoint="list_employees", params=None):
    result = env.call(world, endpoint, params)
    incidents = env.monitor.ingest_result(result)
    assert incidents, "expected drift"
    return incidents[0]


def simulate(env: DriftEnv, world, calls=30, endpoint="list_employees", params=None) -> None:
    """Live traffic during a canary: each call is routed by the change requests store."""
    for _ in range(calls):
        record, canary, arm = env.changes.select_version(env.name)
        result = env.call(world, endpoint, params, version=record.version)
        if canary is not None:
            env.changes.record_call(canary.id, arm, record.version, result)


# --- the happy path ---------------------------------------------------------------------------------


def test_rename_is_patched_mechanically_then_approved_canaried_promoted_and_rolled_back(drift_env):
    env = drift_env
    world = env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name"))
    inc = detect(env, world)

    outcome = env.pipeline(never_called).run(inc.id, env.repair_world(world))
    assert outcome.strategy.startswith("deterministic:rename displayName->display_name")
    assert outcome.rounds == 1 and outcome.report.passed
    assert outcome.triage.drift_class == "schema" and outcome.triage.risk_class == "high"
    change = outcome.change_request
    assert change.status == "pending" and change.verified and not change.auto_approved
    assert (change.base_version, change.candidate_version) == ("0.1.0", "0.1.1")
    assert any(op["path"].endswith("/fields/[target=display_name]/source") and op["to"] == "display_name" for op in change.diff)
    assert env.monitor.get(inc.id).status == "in_repair" and env.monitor.get(inc.id).change_request_id == change.id
    assert env.registry.get_published("bamboohr").version == "0.1.0"  # nothing is live yet
    assert env.registry.get_version("bamboohr", "0.1.1").status == "verified"
    assert env.registry.get_version("bamboohr", "0.1.1").provenance.startswith("repair:deterministic")

    # The gate: no canary and no promotion before a human approves.
    with pytest.raises(ChangeError):
        env.changes.start_canary(change.id, 0.5)
    with pytest.raises(ChangeError):
        env.changes.promote(change.id, "pradhi")
    approved = env.changes.approve(change.id, "pradhi", "schema and mapping move together")
    assert approved.status == "approved" and approved.decided_by == "pradhi"

    # Canary: half the traffic goes to the candidate; the base keeps failing against the drifted API.
    env.changes.start_canary(change.id, 0.5)
    assert env.changes.canary_report(change.id).verdict == "insufficient"
    with pytest.raises(ChangeError):
        env.changes.promote(change.id, "pradhi")
    simulate(env, world, calls=30)
    report = env.changes.canary_report(change.id)
    assert report.verdict == "pass", report.reason
    assert report.base.calls > 0 and report.base.failures == report.base.calls
    assert report.candidate.calls >= 3 and report.candidate.failures == 0

    promoted = env.changes.promote(change.id, "pradhi")
    assert promoted.status == "promoted" and promoted.canary["verdict"] == "pass"
    assert env.registry.get_published("bamboohr").version == "0.1.1"
    assert env.monitor.get(inc.id).status == "resolved"
    assert env.call(world).ok  # production traffic is healthy again

    # Rollback in one step; the rolled-back version can never be re-published.
    restored = env.changes.rollback("bamboohr", "pradhi", "drill")
    assert restored.version == "0.1.0" and restored.status == "published"
    statuses = {v.version: v.status for v in env.registry.list_versions("bamboohr")}
    assert statuses == {"0.1.0": "published", "0.1.1": "rolled_back"}
    assert env.changes.get(change.id).status == "rolled_back"
    assert env.monitor.get(inc.id).status == "needs_human"
    with pytest.raises(Exception):
        env.registry.publish("bamboohr", "0.1.1")


# --- repairs that need the agent ------------------------------------------------------------------


def test_semantic_drift_goes_to_the_agent_with_the_evidence(drift_env):
    env = drift_env
    world = env.world(SetField(endpoint_id="get_employee", field="status", value="OnLeave"))
    inc = detect(env, world, "get_employee", {"id": "1000"})
    agent = scripted(fix_enum)
    outcome = env.pipeline(agent).run(inc.id, env.repair_world(world))
    assert (outcome.triage.drift_class, outcome.triage.risk_class) == ("semantic", "high")
    assert outcome.strategy == "agent:claude-opus-5" and outcome.rounds == 1
    assert outcome.change_request.status == "pending"
    feedback = agent.calls[0]
    assert "class=semantic" in feedback and "'OnLeave' is not one of" in feedback
    assert '"status": "OnLeave"' in feedback  # the sample body
    assert "enum_map" in feedback  # class-specific guidance
    candidate = load_manifest(env.registry.get_version("bamboohr", "0.1.1").manifest)
    status_map = next(f for f in candidate.mapping_for("get_employee").fields if f.target == "employment_status")
    assert status_map.args["map"]["OnLeave"] == "on_leave"


def test_auth_drift_is_high_risk_and_repaired_by_the_agent(drift_env):
    env = drift_env
    world = env.world(RequireHeader(name="X-Api-Key"))
    inc = detect(env, world)
    outcome = env.pipeline(scripted(fix_auth)).run(inc.id, env.repair_world(world))
    assert outcome.triage.drift_class == "auth" and outcome.triage.risk_class == "high"
    assert outcome.change_request.status == "pending" and outcome.report.passed
    assert any(op["path"].startswith("/auth") for op in outcome.change_request.diff)


def test_retired_endpoint_is_repaired_with_its_successor(drift_env):
    env = drift_env
    world = env.world(PathMoved(endpoint_id="list_employees", new_path="/employees/directory/v2"))
    inc = detect(env, world)
    assert inc.kind == "deprecation" and inc.status_code == 410
    agent = scripted(fix_path)
    outcome = env.pipeline(agent).run(inc.id, env.repair_world(world))
    assert outcome.triage.drift_class == "deprecation" and outcome.triage.risk_class == "high"
    assert 'rel="successor-version"' in agent.calls[0]
    assert outcome.change_request.status == "pending" and outcome.report.passed


def test_retyped_field_is_not_a_rename_so_the_agent_handles_it(drift_env):
    env = drift_env
    world = env.world(RetypeField(endpoint_id="list_employees", field="id", value=1000))
    inc = detect(env, world)
    outcome = env.pipeline(scripted(fix_retype)).run(inc.id, env.repair_world(world))
    assert outcome.strategy == "agent:claude-opus-5" and outcome.report.passed
    assert outcome.triage.risk_class == "high"  # id is a mapped field


def test_verification_failures_are_fed_back_for_another_round(drift_env):
    env = drift_env
    world = env.world(SetField(endpoint_id="get_employee", field="status", value="OnLeave"))
    inc = detect(env, world, "get_employee", {"id": "1000"})
    agent = scripted(wrong_fix, fix_enum)
    outcome = env.pipeline(agent, max_rounds=2).run(inc.id, env.repair_world(world))
    assert outcome.rounds == 2 and outcome.report.passed
    assert "still failed verification" in agent.calls[1] and "'OnLeave' is not one of" in agent.calls[1]


def test_a_repair_that_never_verifies_fails_closed(drift_env):
    env = drift_env
    world = env.world(NonJson(endpoint_id="list_employees"))
    inc = detect(env, world)
    outcome = env.pipeline(scripted(unchanged), max_rounds=2).run(inc.id, env.repair_world(world))
    assert outcome.rounds == 2 and not outcome.report.passed
    change = outcome.change_request
    assert change.status == "failed" and not change.verified
    assert env.monitor.get(inc.id).status == "repair_failed"
    assert env.registry.get_version("bamboohr", "0.1.1").status == "rejected"
    assert env.registry.get_published("bamboohr").version == "0.1.0"
    with pytest.raises(ChangeError):
        env.changes.approve(change.id, "pradhi")


# --- things that must not be auto-repaired ------------------------------------------------------------


def test_sunset_notice_is_routed_to_a_human(drift_env):
    env = drift_env
    world = env.world(Sunset(endpoint_id="list_employees", successor="/v2/employees"))
    result = env.call(world)
    assert result.ok
    inc = env.monitor.ingest_result(result)[0]
    outcome = env.pipeline(never_called).run(inc.id, env.repair_world(world))
    assert outcome.change_request is None and not outcome.triage.repairable
    assert env.monitor.get(inc.id).status == "needs_human"


def test_upstream_failures_are_transient_not_repaired(drift_env):
    env = drift_env
    world = env.world(StatusResponse(endpoint_id="list_employees", status_code=503))
    inc = detect(env, world)
    outcome = env.pipeline(never_called).run(inc.id, env.repair_world(world))
    assert outcome.triage.drift_class == "transient" and outcome.change_request is None
    assert env.monitor.get(inc.id).status == "triaged"


def test_rerunning_a_repair_returns_the_open_change_request(drift_env):
    env = drift_env
    world = env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name"))
    inc = detect(env, world)
    pipeline = env.pipeline(never_called)
    first = pipeline.run(inc.id, env.repair_world(world))
    second = pipeline.run(inc.id, env.repair_world(world))
    assert first.change_request.id == second.change_request.id
    assert len(env.registry.list_versions("bamboohr")) == 2


# --- policy, canary failure, worker -----------------------------------------------------------------


def test_policy_auto_approves_only_the_allowed_risk_class(drift_env):
    env = drift_env
    env.changes.set_policy("bamboohr", "medium", True, actor="pradhi")
    assert env.changes.get_policy("bamboohr").auto_approve == {"low": False, "medium": True, "high": False}

    moved = env.world(WrapItems(endpoint_id="list_employees", key="data"))  # medium: no mapped field touched
    outcome = env.pipeline(never_called).run(detect(env, moved).id, env.repair_world(moved))
    assert outcome.triage.risk_class == "medium"
    assert outcome.change_request.status == "approved" and outcome.change_request.auto_approved
    assert outcome.change_request.decided_by == "policy"
    env.changes.reject(outcome.change_request.id, "pradhi", "clearing the deck")

    # A different endpoint, so the events do not fold into the incident a human is already holding.
    renamed = env.world(RenameField(endpoint_id="get_employee", old="id", new="employee_id"))  # high: id is mapped
    outcome = env.pipeline(never_called).run(detect(env, renamed, "get_employee", {"id": "1000"}).id, env.repair_world(renamed))
    assert outcome.triage.risk_class == "high"
    assert outcome.change_request.status == "pending" and not outcome.change_request.auto_approved


def test_canary_that_regresses_is_aborted_and_base_stays_live(drift_env):
    env = drift_env
    world = env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name"))
    inc = detect(env, world)
    change = env.pipeline(never_called).run(inc.id, env.repair_world(world)).change_request
    env.changes.approve(change.id, "pradhi")
    env.changes.start_canary(change.id, 0.5)
    # The world changes again under the canary: everything starts failing.
    world.scenario.mutations.append(StatusResponse(endpoint_id="list_employees", status_code=500))
    simulate(env, world, calls=20)
    report = env.changes.canary_report(change.id)
    assert report.verdict == "fail" and "exceeds" in report.reason
    with pytest.raises(ChangeError):
        env.changes.promote(change.id, "pradhi")
    aborted = env.changes.abort_canary(change.id, "pradhi")
    assert aborted.status == "aborted"
    assert env.registry.get_published("bamboohr").version == "0.1.0"
    assert env.monitor.get(inc.id).status == "repair_failed"
    with pytest.raises(ChangeError):
        env.changes.promote(change.id, "pradhi", force=True)  # aborted is final; force only overrides a verdict


def test_worker_tick_drives_the_loop_without_publishing_unapproved_changes(drift_env):
    env = drift_env
    world = env.world(WrapItems(endpoint_id="list_employees", key="data"))
    inc = detect(env, world)
    worker = DriftWorker(env.monitor, env.changes, env.pipeline(never_called), lambda incident: env.repair_world(world), canary_fraction=0.5)

    first = worker.tick()
    assert first.triaged == [inc.id] and first.repaired == [inc.id]
    change = env.changes.get(env.monitor.get(inc.id).change_request_id)
    assert change.status == "pending" and first.canaries_started == [] and first.promoted == []
    assert env.registry.get_published("bamboohr").version == "0.1.0"

    env.changes.approve(change.id, "pradhi")
    second = worker.tick()
    assert second.canaries_started == [change.id] and second.waiting == [change.id]
    simulate(env, world, calls=30)
    third = worker.tick()
    assert third.promoted == [change.id]
    assert env.registry.get_published("bamboohr").version == "0.1.1"
    assert env.monitor.get(inc.id).status == "resolved"


def test_worker_reports_when_no_world_is_available(drift_env):
    env = drift_env
    inc = detect(env, env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name")))
    worker = DriftWorker(env.monitor, env.changes, env.pipeline(never_called), lambda incident: None)
    tick = worker.tick()
    assert tick.triaged == [inc.id] and tick.repaired == []
    assert "no connection" in tick.errors[f"incident:{inc.id}"]
    assert env.monitor.get(inc.id).status == "triaged"


@pytest.mark.skipif(not has_anthropic_credentials(), reason="needs ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
def test_real_agent_repairs_semantic_drift(drift_env):
    env = drift_env
    world = env.world(SetField(endpoint_id="get_employee", field="status", value="OnLeave"))
    inc = detect(env, world, "get_employee", {"id": "1000"})
    pipeline = RepairPipeline(env.registry, env.monitor, env.changes)
    outcome = pipeline.run(inc.id, env.repair_world(world))
    print(outcome.report.summary())
    assert outcome.report.passed, outcome.report.summary()
    candidate = load_manifest(env.registry.get_version("bamboohr", outcome.change_request.candidate_version).manifest)
    status_map = next(f for f in candidate.mapping_for("get_employee").fields if f.target == "employment_status")
    assert any(k.lower() == "onleave" for k in status_map.args["map"])
