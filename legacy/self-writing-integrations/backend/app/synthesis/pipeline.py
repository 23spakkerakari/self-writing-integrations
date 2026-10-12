"""Synthesize -> verify -> store. The loop feeds verification failures back to the agent as
repair feedback, bounded by `max_rounds`. The stored version ends up `verified` (ready for
a human to publish) or `rejected` (with the report attached for review)."""
from __future__ import annotations

from typing import Any

import anthropic
from pydantic import BaseModel

from app.manifest.schema import IntegrationManifest
from app.registry.store import Registry, VersionRecord
from app.synthesis.agent import synthesize
from app.verification.harness import VerificationReport, verify


class PipelineResult(BaseModel):
    record: VersionRecord
    report: VerificationReport
    rounds: int
    model_attempts: int


def synthesize_and_verify(
    registry: Registry,
    spec_text: str,
    name_hint: str,
    model: str = "claude-opus-5",
    max_attempts: int = 3,
    max_rounds: int = 2,
    client: anthropic.Anthropic | None = None,
    synthesize_fn: Any = synthesize,
) -> PipelineResult:
    manifest: IntegrationManifest | None = None
    report: VerificationReport | None = None
    model_attempts = 0
    rounds = 0
    for rounds in range(1, max_rounds + 1):
        feedback = _feedback(report) if report is not None else None
        result = synthesize_fn(
            spec_text, name_hint, model=model, max_attempts=max_attempts, client=client, previous=manifest, feedback=feedback
        )
        model_attempts += result.attempts
        manifest = result.manifest
        report = verify(manifest, mode="mock")
        if report.passed:
            break

    assert manifest is not None and report is not None
    manifest = manifest.model_copy(update={"version": registry.next_version(manifest.name)})
    report = report.model_copy(update={"version": manifest.version})
    registry.save_snapshot(manifest.name, spec_text)
    registry.create_version(manifest, spec_source=spec_text, provenance=f"synthesis:{model}")
    record = registry.record_verification(manifest.name, manifest.version, report.model_dump(mode="json"), report.passed)
    return PipelineResult(record=record, report=report, rounds=rounds, model_attempts=model_attempts)


def _feedback(report: VerificationReport) -> str:
    lines: list[str] = []
    for check in report.checks:
        if not check.passed:
            lines.append(f"endpoint '{check.endpoint_id}' (HTTP {check.status_code}):")
            lines.extend(f"  - {e}" for e in check.errors)
    return "\n".join(lines) or "verification failed without endpoint-level errors"
