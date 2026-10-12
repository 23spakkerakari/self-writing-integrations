"""Deterministic triage.

The risk class gates the approval policy, so classification has to be auditable and
reproducible. Rules, not a model, decide how much supervision a repair gets; the model is
used for the repair itself.

Risk classes:
- low: a policy knob (rate limit, retry) or a change that cannot alter data.
- medium: response shape changes that do not touch a canonical mapping.
- high: anything touching auth, removed or retyped mapped fields, canonical mappings, or a
  retired endpoint. High stays behind a human until trust is earned per integration.
"""
from __future__ import annotations

import re

from app.drift.models import DriftIncident, Triage
from app.manifest.schema import IntegrationManifest

_REQUIRED = re.compile(r"'([^']+)' is a required property")
_ENUM = re.compile(r"is not one of")
_TYPE = re.compile(r"is not of type")
_STRUCTURAL = re.compile(r"(paths|properties|required|type|parameters|security|enum|\$ref|\bin:|schema)", re.I)


def triage(incident: DriftIncident, manifest: IntegrationManifest | None = None) -> Triage:
    kind, code = incident.kind, incident.status_code
    if kind == "transport_error":
        return Triage(
            drift_class="transient",
            risk_class="low",
            repairable=False,
            rationale="connection failures; the gateway retries these, there is nothing to patch",
        )
    if kind == "unexpected_status":
        if code in (401, 403):
            return Triage(
                drift_class="auth",
                risk_class="high",
                repairable=True,
                rationale=f"HTTP {code}: credentials or auth scheme rejected; auth changes always need approval",
            )
        if code == 404:
            return Triage(
                drift_class="deprecation",
                risk_class="high",
                repairable=True,
                rationale="HTTP 404: the path no longer exists; the endpoint may have moved",
            )
        if code == 429:
            return Triage(
                drift_class="behavioral",
                risk_class="low",
                repairable=True,
                rationale="HTTP 429 after retries: the rate limit tightened; lowering requests_per_second is low risk",
            )
        if code is not None and code >= 500:
            return Triage(
                drift_class="transient",
                risk_class="low",
                repairable=False,
                rationale=f"HTTP {code}: upstream failure; monitor rather than patch",
            )
        return Triage(drift_class="behavioral", risk_class="medium", repairable=True, rationale=f"HTTP {code}: unexpected status")
    if kind == "deprecation":
        if code == 410:
            return Triage(
                drift_class="deprecation",
                risk_class="high",
                repairable=True,
                rationale="HTTP 410 Gone: the endpoint was retired; its successor must be adopted",
            )
        return Triage(
            drift_class="deprecation",
            risk_class="medium",
            repairable=False,
            rationale="Sunset/Deprecation notice on a working endpoint; a human should plan the migration before it retires",
        )
    if kind == "malformed_body":
        return Triage(drift_class="behavioral", risk_class="medium", repairable=True, rationale="the endpoint returned a non-JSON body")
    if kind == "pagination_runaway":
        return Triage(
            drift_class="behavioral",
            risk_class="medium",
            repairable=True,
            rationale="pagination no longer terminates; the style, parameter names or page size may have changed",
        )
    if kind == "mapping_error":
        return Triage(
            drift_class="semantic",
            risk_class="high",
            repairable=True,
            rationale="records no longer map onto the canonical object; a field's meaning or format may have changed",
        )
    if kind == "spec_changed":
        return _triage_spec_change(incident)
    if kind == "schema_violation":
        return _triage_schema(incident, manifest)
    return Triage(drift_class="behavioral", risk_class="medium", repairable=True, rationale=f"unclassified drift kind '{kind}'")


def _triage_schema(incident: DriftIncident, manifest: IntegrationManifest | None) -> Triage:
    samples = incident.samples
    if any(_ENUM.search(s) for s in samples):
        return Triage(
            drift_class="semantic",
            risk_class="high",
            repairable=True,
            rationale="a field carries a value outside its enum; the new value's meaning has to be mapped",
        )
    fields = fields_in_samples(samples)
    mapped = mapped_sources(manifest, incident.endpoint_id) if manifest is not None else set()
    touched = sorted(f for f in fields if f in mapped or any(m.startswith(f + ".") for m in mapped))
    if touched:
        return Triage(
            drift_class="schema",
            risk_class="high",
            repairable=True,
            rationale=f"schema change touches mapped fields {touched}; the canonical mapping must change with it",
        )
    if any(_TYPE.search(s) for s in samples):
        return Triage(drift_class="schema", risk_class="medium", repairable=True, rationale="a field changed type")
    if fields:
        return Triage(
            drift_class="schema", risk_class="medium", repairable=True, rationale=f"required fields missing: {sorted(fields)}"
        )
    return Triage(drift_class="schema", risk_class="medium", repairable=True, rationale="response no longer matches the stored schema")


def _triage_spec_change(incident: DriftIncident) -> Triage:
    diff = incident.sample_body.get("diff", []) if isinstance(incident.sample_body, dict) else []
    changed = [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    if changed and not any(_STRUCTURAL.search(line) for line in changed):
        return Triage(
            drift_class="cosmetic",
            risk_class="low",
            repairable=False,
            rationale="the specification text changed without structural changes (descriptions, examples)",
        )
    return Triage(
        drift_class="schema",
        risk_class="medium",
        repairable=True,
        rationale="the specification's structure changed; the manifest should be re-derived against it",
    )


def fields_in_samples(samples: list[str]) -> set[str]:
    """Field names named by validation messages: missing required properties and type mismatches."""
    out: set[str] = set()
    for sample in samples:
        match = _REQUIRED.search(sample)
        if match:
            out.add(match.group(1))
            continue
        if _TYPE.search(sample):
            location = sample.split(":", 1)[0].strip()
            leaf = location.rsplit("/", 1)[-1]
            if leaf and leaf != "$" and not leaf.isdigit():
                out.add(leaf)
    return out


def mapped_sources(manifest: IntegrationManifest, endpoint_id: str) -> set[str]:
    mapping = manifest.mapping_for(endpoint_id)
    if mapping is None:
        return set()
    sources: set[str] = set()
    for field in mapping.fields:
        if field.source:
            sources.add(field.source)
        paths = field.args.get("paths")
        if isinstance(paths, list):
            sources.update(str(p) for p in paths)
    return sources
