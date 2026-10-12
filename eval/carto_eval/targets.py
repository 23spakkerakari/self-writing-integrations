"""The spec 18.4 metric table as data: ids, labels, targets, comparison direction and units.

The ids are the keys of ``EvalReport.metrics`` and of the history records; the engine milestones
report deltas against this table (CLAUDE.md: "Engine changes ... must run ``make eval``").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

Comparison = Literal[">=", "<="]
Unit = Literal["ratio", "seconds", "per_flow_day"]
Status = Literal["pass", "fail", "n/a"]

LINK_PRECISION_EXACT_BRIDGE: Final = "link_precision_exact_bridge"
LINK_RECALL_EXACT_BRIDGE: Final = "link_recall_exact_bridge"
LINK_PRECISION_COMPOSITE: Final = "link_precision_composite"
LINK_RECALL_COMPOSITE: Final = "link_recall_composite"
ENTITY_PURITY: Final = "entity_purity"
TRANSACTION_PAIRWISE_F1: Final = "transaction_pairwise_f1"
BATCH_KEY_PRECISION: Final = "batch_key_precision"
BATCH_KEY_RECALL: Final = "batch_key_recall"
FAULT_DETECTION_RECALL: Final = "fault_detection_recall"
TIME_TO_DETECT_P95_SECONDS: Final = "time_to_detect_p95_seconds"
FALSE_ALERTS_PER_FLOW_DAY: Final = "false_alerts_per_flow_day"
VISIBILITY_GAP_ATTRIBUTION: Final = "visibility_gap_attribution"
MANUAL_HOP_PRECISION: Final = "manual_hop_precision"
MANUAL_HOP_RECALL: Final = "manual_hop_recall"
LIKELY_CAUSE_TOP1: Final = "likely_cause_top1"

DEFAULT_TOLERANCE_POINTS: Final = 2.0
"""Spec 18.4: PRs fail on regressions beyond tolerance, default 2 points (0.02 on a ratio)."""
DEFAULT_UNIT_TOLERANCE: Final = 1.0
"""Tolerance for metrics measured in seconds or alerts per flow-day: one unit."""


@dataclass(frozen=True, slots=True)
class MetricTarget:
    """One row of the spec 18.4 table."""

    metric_id: str
    label: str
    target: float
    comparison: Comparison
    unit: Unit

    def evaluate(self, value: float | None) -> Status:
        """``pass`` or ``fail`` against the target; ``n/a`` when the metric is undefined."""
        if value is None:
            return "n/a"
        met = value >= self.target if self.comparison == ">=" else value <= self.target
        return "pass" if met else "fail"

    def worsening(self, previous: float, current: float) -> float:
        """How far ``current`` moved in the bad direction from ``previous`` (negative: better)."""
        return previous - current if self.comparison == ">=" else current - previous

    def tolerance(
        self,
        points: float = DEFAULT_TOLERANCE_POINTS,
        unit_tolerance: float = DEFAULT_UNIT_TOLERANCE,
    ) -> float:
        """Regression tolerance: ``points / 100`` on a ratio, ``unit_tolerance`` otherwise."""
        return points / 100.0 if self.unit == "ratio" else unit_tolerance

    def format_value(self, value: float | None) -> str:
        """Render a value in the unit of the row (``n/a`` when undefined)."""
        if value is None:
            return "n/a"
        if self.unit == "ratio":
            return f"{value:.4f}"
        if self.unit == "seconds":
            return f"{value:.1f} s"
        return f"{value:.2f}"

    def format_target(self) -> str:
        """Render the target with its comparison, e.g. ``>= 0.95`` or ``<= 120 s``."""
        if self.unit == "ratio":
            return f"{self.comparison} {self.target:.2f}"
        if self.unit == "seconds":
            return f"{self.comparison} {self.target:.0f} s"
        return f"{self.comparison} {self.target:.2f}"


TARGETS: Final[tuple[MetricTarget, ...]] = (
    MetricTarget(
        LINK_PRECISION_EXACT_BRIDGE, "Exact and bridge link precision", 0.95, ">=", "ratio"
    ),
    MetricTarget(LINK_RECALL_EXACT_BRIDGE, "Exact and bridge link recall", 0.90, ">=", "ratio"),
    MetricTarget(LINK_PRECISION_COMPOSITE, "Composite link precision", 0.85, ">=", "ratio"),
    MetricTarget(LINK_RECALL_COMPOSITE, "Composite link recall", 0.70, ">=", "ratio"),
    MetricTarget(ENTITY_PURITY, "Entity family purity", 0.95, ">=", "ratio"),
    MetricTarget(TRANSACTION_PAIRWISE_F1, "Transaction pairwise F1", 0.95, ">=", "ratio"),
    MetricTarget(BATCH_KEY_PRECISION, "Batch key detection precision", 0.95, ">=", "ratio"),
    MetricTarget(BATCH_KEY_RECALL, "Batch key detection recall", 0.90, ">=", "ratio"),
    MetricTarget(FAULT_DETECTION_RECALL, "Injected fault detection recall", 1.0, ">=", "ratio"),
    MetricTarget(
        TIME_TO_DETECT_P95_SECONDS, "Time to detect after deadline (p95)", 120.0, "<=", "seconds"
    ),
    MetricTarget(
        FALSE_ALERTS_PER_FLOW_DAY,
        "False alerts on fault-free days (per flow per day)",
        1.0,
        "<=",
        "per_flow_day",
    ),
    MetricTarget(
        VISIBILITY_GAP_ATTRIBUTION, "Visibility gap correctly attributed", 1.0, ">=", "ratio"
    ),
    MetricTarget(MANUAL_HOP_PRECISION, "Manual hop precision", 0.80, ">=", "ratio"),
    MetricTarget(MANUAL_HOP_RECALL, "Manual hop recall", 0.80, ">=", "ratio"),
    MetricTarget(
        LIKELY_CAUSE_TOP1, "Likely cause top-1 accuracy on injected faults", 0.70, ">=", "ratio"
    ),
)

TARGETS_BY_ID: Final[Mapping[str, MetricTarget]] = {target.metric_id: target for target in TARGETS}
