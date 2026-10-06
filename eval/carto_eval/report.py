"""Eval report: JSON and Markdown output plus the CI history with regression detection (spec 18.4).

"Output: JSON plus a Markdown report. CI stores history; PRs touching the engine get a comment
with deltas and fail on regressions beyond tolerance (default 2 points)." The history is one
JSON line per run in ``history.ndjson`` next to the reports; a run is compared with the previous
record of the same scenario, seed and day count, the only runs whose numbers are comparable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from carto_eval.targets import (
    DEFAULT_TOLERANCE_POINTS,
    DEFAULT_UNIT_TOLERANCE,
    TARGETS,
    TARGETS_BY_ID,
    Comparison,
    MetricTarget,
    Status,
    Unit,
)

HISTORY_FILE = "history.ndjson"
FLOAT_SLACK = 1e-9
"""Slack on the tolerance comparison so a move of exactly the tolerance never regresses."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "report timestamps must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


class MetricResult(_Model):
    """One scored row of the spec 18.4 table."""

    label: str
    value: float | None
    target: float
    comparison: Comparison
    unit: Unit
    status: Status


class ReportCounts(_Model):
    """What was scored, so a reader can tell a tiny scenario from the default run."""

    events: int = Field(description="Records in event_txn.ndjson, noise included.")
    transactions: int
    links: int
    entities: int
    batches: int
    faults: int
    alerts: int = Field(description="Predicted alerts.")


class HistoryRecord(_Model):
    """One line of ``history.ndjson``."""

    scenario: str
    seed: int
    days: int
    generated_at: datetime
    predictions_present: bool
    metrics: dict[str, float | None]

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class EvalReport(_Model):
    """The result of one harness run: every metric with its target and status."""

    scenario: str
    seed: int
    days: int
    generated_at: datetime = Field(description="UTC; set by the CLI, never by library code.")
    predictions_present: bool
    counts: ReportCounts
    metrics: dict[str, MetricResult]
    regressions: list[str] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    def failed_metrics(self) -> list[str]:
        """Ids of the metrics whose status is ``fail``."""
        return [metric_id for metric_id, result in self.metrics.items() if result.status == "fail"]

    def with_regressions(self, regressions: Iterable[str]) -> Self:
        """A copy carrying ``regressions``."""
        return self.model_copy(update={"regressions": list(regressions)})

    def history_record(self) -> HistoryRecord:
        """The line this run appends to ``history.ndjson``."""
        return HistoryRecord(
            scenario=self.scenario,
            seed=self.seed,
            days=self.days,
            generated_at=self.generated_at,
            predictions_present=self.predictions_present,
            metrics={metric_id: result.value for metric_id, result in self.metrics.items()},
        )

    def to_json(self) -> str:
        """Sorted keys, two-space indent, trailing LF."""
        payload = self.model_dump(mode="json")
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def to_markdown(self) -> str:
        """A heading, the run facts, one table (label, value, target, status), regressions."""
        counts = self.counts
        predictions = (
            "Predictions: present (engine output scored)."
            if self.predictions_present
            else "Predictions: not present (the empty prediction scored; "
            "a metric with nothing to score reads n/a)."
        )
        generated_at = self.generated_at.isoformat().replace("+00:00", "Z")
        facts = (
            f"Scenario `{self.scenario}`, seed {self.seed}, {self.days} days, "
            f"generated at {generated_at}."
        )
        count_line = (
            f"Counts: {counts.events:,} events, {counts.transactions:,} transactions, "
            f"{counts.links} links, {counts.entities} entities, {counts.batches} batches, "
            f"{counts.faults} faults, {counts.alerts} alerts."
        )
        lines = [
            f"# carto eval: {self.scenario}",
            "",
            facts,
            predictions,
            count_line,
            "",
            "| Metric | Value | Target | Status |",
            "| --- | --- | --- | --- |",
        ]
        for metric_id, result in self.metrics.items():
            target = _target_for(metric_id, result)
            lines.append(
                f"| {result.label} | {target.format_value(result.value)} | "
                f"{target.format_target()} | {result.status} |"
            )
        lines.append("")
        if self.regressions:
            lines.append("Regressions against the previous run of this scenario:")
            lines.extend(f"- {regression}" for regression in self.regressions)
        else:
            lines.append("Regressions against the previous run of this scenario: none.")
        return "\n".join(lines) + "\n"

    def write_json(self, path: Path) -> None:
        """Write :meth:`to_json` with LF line endings."""
        path.write_text(self.to_json(), encoding="utf-8", newline="\n")

    def write_markdown(self, path: Path) -> None:
        """Write :meth:`to_markdown` with LF line endings."""
        path.write_text(self.to_markdown(), encoding="utf-8", newline="\n")


def _target_for(metric_id: str, result: MetricResult) -> MetricTarget:
    """The table row for a metric; a report row unknown to the table keeps its own facts."""
    known = TARGETS_BY_ID.get(metric_id)
    if known is not None:
        return known
    return MetricTarget(metric_id, result.label, result.target, result.comparison, result.unit)


def build_report(
    *,
    scenario: str,
    seed: int,
    days: int,
    generated_at: datetime,
    predictions_present: bool,
    counts: ReportCounts,
    values: Mapping[str, float | None],
) -> EvalReport:
    """Attach targets and statuses to raw metric values, in table order.

    ``values`` must hold every metric id of the table; an unknown id is a programming error.
    """
    unknown = set(values) - set(TARGETS_BY_ID)
    if unknown:
        msg = f"values for metrics not in the spec 18.4 table: {sorted(unknown)}"
        raise KeyError(msg)
    metrics = {
        target.metric_id: MetricResult(
            label=target.label,
            value=values.get(target.metric_id),
            target=target.target,
            comparison=target.comparison,
            unit=target.unit,
            status=target.evaluate(values.get(target.metric_id)),
        )
        for target in TARGETS
    }
    return EvalReport(
        scenario=scenario,
        seed=seed,
        days=days,
        generated_at=generated_at,
        predictions_present=predictions_present,
        counts=counts,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------------------------
# History and regressions
# ---------------------------------------------------------------------------------------------


def read_history(path: Path) -> list[HistoryRecord]:
    """Every record in ``history.ndjson``; a missing file is an empty history."""
    if not path.is_file():
        return []
    records: list[HistoryRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(HistoryRecord.model_validate_json(line))
            except ValidationError as exc:
                msg = f"{path}: line {line_no}: not a history record ({exc.errors()[0]['msg']})"
                raise ValueError(msg) from exc
    return records


def last_record(
    records: Iterable[HistoryRecord], scenario: str, seed: int, days: int
) -> HistoryRecord | None:
    """The most recent record of ``scenario`` with the same ``seed`` and ``days``, or None.

    A run over another seed or day count scores different data, so comparing with it would
    report moves that are not regressions.
    """
    previous = None
    for record in records:
        if (record.scenario, record.seed, record.days) == (scenario, seed, days):
            previous = record
    return previous


def append_history(path: Path, record: HistoryRecord) -> None:
    """Append one line (the parent directory must exist)."""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(record.model_dump_json())
        handle.write("\n")


def find_regressions(
    previous: HistoryRecord | None,
    report: EvalReport,
    *,
    tolerance_points: float = DEFAULT_TOLERANCE_POINTS,
    unit_tolerance: float = DEFAULT_UNIT_TOLERANCE,
) -> list[str]:
    """Metrics that moved in the bad direction by more than the tolerance since ``previous``.

    A ratio regresses when it drops (or rises, for a ``<=`` target) by more than
    ``tolerance_points / 100``; a count or duration by more than ``unit_tolerance``. A move of
    exactly the tolerance is not a regression (compared with a 1e-9 slack, so 1.0 to 0.98 is two
    points, not 0.020000000000000018). A metric that was defined and is now n/a regresses; one
    that was n/a and is now defined does not. No previous record: no regressions.
    """
    if previous is None:
        return []
    regressions: list[str] = []
    for metric_id, result in report.metrics.items():
        if metric_id not in previous.metrics:
            continue
        before = previous.metrics[metric_id]
        after = result.value
        target = _target_for(metric_id, result)
        if before is None:
            continue
        if after is None:
            regressions.append(
                f"{metric_id}: {target.format_value(before)} -> n/a (no longer measurable)"
            )
            continue
        tolerance = target.tolerance(tolerance_points, unit_tolerance)
        worsening = target.worsening(before, after)
        if worsening > tolerance + FLOAT_SLACK:
            regressions.append(
                f"{metric_id}: {target.format_value(before)} -> {target.format_value(after)} "
                f"(worse by {target.format_value(worsening)}, "
                f"tolerance {target.format_value(tolerance)})"
            )
    return regressions
