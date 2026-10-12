"""Incident aggregation, spec re-fetch diffs, the triage rule table, manifest diffs and the
mechanical rename patcher."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.drift.diff import manifest_diff, summarize
from app.drift.models import DriftIncident
from app.drift.patches import deterministic_repair
from app.drift.triage import triage
from app.manifest.schema import load_manifest
from app.verification.drift_scenarios import RemoveField, RenameField, RetypeField, WrapItems
from app.verification.harness import verify

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def incident(kind, samples=(), status_code=None, sample_body=None, endpoint_id="list_employees"):
    return DriftIncident(
        id=1,
        integration="bamboohr",
        endpoint_id=endpoint_id,
        kind=kind,
        status="open",
        version="0.1.0",
        status_code=status_code,
        first_seen=NOW,
        last_seen=NOW,
        count=1,
        samples=list(samples),
        sample_body=sample_body,
    )


# --- aggregation ------------------------------------------------------------------------------


def test_repeated_events_fold_into_one_incident(drift_env):
    world = drift_env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name"))
    first = drift_env.monitor.ingest_result(drift_env.call(world))
    second = drift_env.monitor.ingest_result(drift_env.call(world))
    assert [i.id for i in first] == [i.id for i in second]
    inc = second[0]
    assert inc.kind == "schema_violation" and inc.status == "open" and inc.version == "0.1.0"
    assert inc.count == 2  # two calls, not six validation messages
    assert len(inc.samples) == 3 and all("'displayName' is a required property" in s for s in inc.samples)
    assert inc.sample_body["employees"][0]["display_name"]
    assert inc.status_code == 200


def test_each_kind_on_an_endpoint_is_its_own_incident(drift_env):
    world = drift_env.world(RemoveField(endpoint_id="list_employees", field="id"))
    incidents = drift_env.monitor.ingest_result(drift_env.call(world))
    assert sorted(i.kind for i in incidents) == ["mapping_error", "schema_violation"]
    assert len(drift_env.monitor.list(active_only=True)) == 2


def test_resolved_incidents_are_not_reused(drift_env):
    world = drift_env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name"))
    first = drift_env.monitor.ingest_result(drift_env.call(world))[0]
    drift_env.monitor.resolve(first.id, "fixed")
    again = drift_env.monitor.ingest_result(drift_env.call(world))[0]
    assert again.id != first.id and again.count == 1
    assert drift_env.monitor.get(first.id).status == "resolved"
    assert [i.id for i in drift_env.monitor.list(status="open")] == [again.id]


def test_spec_check_opens_an_incident_only_when_the_spec_changes(drift_env):
    monitor = drift_env.monitor
    assert monitor.check_spec("bamboohr", "openapi: 3.0.0\ninfo:\n  title: A\n") is None  # baseline
    assert monitor.check_spec("bamboohr", "openapi: 3.0.0\ninfo:\n  title: A\n") is None  # unchanged
    inc = monitor.check_spec("bamboohr", "openapi: 3.0.0\ninfo:\n  title: A\n  description: hello\n")
    assert inc is not None and inc.kind == "spec_changed" and inc.endpoint_id == "*"
    assert "1 changed line" in inc.samples[0]
    assert any(line.startswith("+") and "description: hello" in line for line in inc.sample_body["diff"])
    # A further change while the incident is open refreshes the diff sample.
    again = monitor.check_spec("bamboohr", "openapi: 3.0.0\ninfo:\n  title: A\npaths:\n  /x: {}\n")
    assert again.id == inc.id and again.count == 2
    assert any("paths:" in line for line in again.sample_body["diff"])


# --- triage rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inc, drift_class, risk, repairable",
    [
        (incident("transport_error"), "transient", "low", False),
        (incident("unexpected_status", status_code=401), "auth", "high", True),
        (incident("unexpected_status", status_code=403), "auth", "high", True),
        (incident("unexpected_status", status_code=404), "deprecation", "high", True),
        (incident("unexpected_status", status_code=429), "behavioral", "low", True),
        (incident("unexpected_status", status_code=503), "transient", "low", False),
        (incident("deprecation", status_code=410), "deprecation", "high", True),
        (incident("deprecation", status_code=200), "deprecation", "medium", False),
        (incident("malformed_body"), "behavioral", "medium", True),
        (incident("pagination_runaway"), "behavioral", "medium", True),
        (incident("mapping_error"), "semantic", "high", True),
        (incident("schema_violation", ["status: 'OnLeave' is not one of ['Active', 'Inactive', None]"]), "semantic", "high", True),
        (incident("schema_violation", ["employees/0: 'displayName' is a required property"]), "schema", "high", True),
        (incident("schema_violation", ["$: 'employees' is a required property"]), "schema", "medium", True),
        (incident("schema_violation", ["employees/0/photoUploaded: 'x' is not of type 'boolean'"]), "schema", "medium", True),
        (incident("schema_violation", ["employees/0/id: 1000 is not of type 'string'"]), "schema", "high", True),
        (incident("spec_changed", sample_body={"diff": ["-  description: old", "+  description: new"]}), "cosmetic", "low", False),
        (incident("spec_changed", sample_body={"diff": ["+      properties:", "+        badge: {type: string}"]}), "schema", "medium", True),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_triage_rules(manifest, inc, drift_class, risk, repairable):
    result = triage(inc, manifest)
    assert (result.drift_class, result.risk_class, result.repairable) == (drift_class, risk, repairable), result.rationale


def test_triage_names_the_mapped_fields_it_protects(manifest):
    result = triage(incident("schema_violation", ["employees/0: 'displayName' is a required property"]), manifest)
    assert "displayName" in result.rationale and "mapping" in result.rationale


# --- manifest diff ------------------------------------------------------------------------------


def test_manifest_diff_aligns_lists_by_identity(manifest_dict):
    changed = load_manifest(manifest_dict).model_dump(mode="json")
    changed["endpoints"].reverse()  # reordering alone is not a change
    changed["mappings"][0]["fields"][1]["source"] = "display_name"
    changed["rate_limit"]["requests_per_second"] = 2
    ops = manifest_diff(manifest_dict, changed)
    paths = {op["path"]: op for op in ops}
    assert "/mappings/[endpoint_id=list_employees]/fields/[target=display_name]/source" in paths
    assert paths["/rate_limit/requests_per_second"]["to"] == 2
    assert len(ops) == 2
    assert "display_name" in summarize(ops)


# --- mechanical rename patcher ---------------------------------------------------------------------


def test_rename_patch_rewrites_schema_and_mapping_together(drift_env):
    world = drift_env.world(RenameField(endpoint_id="list_employees", old="displayName", new="display_name"))
    inc = drift_env.monitor.ingest_result(drift_env.call(world))[0]
    patched = deterministic_repair(drift_env.published(), inc)
    assert patched is not None
    candidate, description = patched
    assert description.startswith("rename displayName->display_name")
    props = candidate.endpoint("list_employees").response_schema["properties"]["employees"]["items"]
    assert "display_name" in props["properties"] and "displayName" not in props["properties"]
    assert "display_name" in props["required"]
    field = next(f for f in candidate.mapping_for("list_employees").fields if f.target == "display_name")
    assert field.source == "display_name"
    report = verify(candidate, mode="live", connection=drift_env.connection, secrets=drift_env.secrets, transport=world.transport(), sleep=lambda s: None)
    assert report.passed, report.summary()


def test_rename_patch_moves_items_path_when_the_list_key_changes(drift_env):
    world = drift_env.world(WrapItems(endpoint_id="list_employees", key="data"))
    inc = drift_env.monitor.ingest_result(drift_env.call(world))[0]
    patched = deterministic_repair(drift_env.published(), inc)
    assert patched is not None
    candidate, _ = patched
    assert candidate.endpoint("list_employees").items_path == "data"
    assert "data" in candidate.endpoint("list_employees").response_schema["properties"]
    assert candidate.mapping_for("list_employees").fields[0].source == "id"  # record-level mapping untouched
    report = verify(candidate, mode="live", connection=drift_env.connection, secrets=drift_env.secrets, transport=world.transport(), sleep=lambda s: None)
    assert report.passed, report.summary()


def test_patcher_declines_anything_ambiguous(drift_env):
    removed = drift_env.world(RemoveField(endpoint_id="list_employees", field="displayName"))
    inc = next(i for i in drift_env.monitor.ingest_result(drift_env.call(removed)) if i.kind == "schema_violation")
    assert deterministic_repair(drift_env.published(), inc) is None  # nothing appeared in its place

    retyped = drift_env.world(RetypeField(endpoint_id="list_employees", field="id", value=1000))
    inc = drift_env.monitor.ingest_result(drift_env.call(retyped))[0]
    assert deterministic_repair(drift_env.published(), inc) is None  # not a rename
