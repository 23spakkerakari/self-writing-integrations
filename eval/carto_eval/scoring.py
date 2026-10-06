"""Scores one prediction against one ground truth: every spec 18.4 metric in one pass.

The CLI (``carto-eval run`` and ``self-check``) and the tests call :func:`score`; the metric
functions themselves live in :mod:`carto_eval.metrics`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from carto_eval import metrics
from carto_eval.metrics import (
    CauseResult,
    FalseAlertResult,
    FaultDetectionResult,
    PairwiseResult,
    PrecisionRecall,
    PurityResult,
)
from carto_eval.predictions import PredictedAlert, Predictions
from carto_eval.report import EvalReport, ReportCounts
from carto_eval.targets import (
    BATCH_KEY_PRECISION,
    BATCH_KEY_RECALL,
    ENTITY_PURITY,
    FALSE_ALERTS_PER_FLOW_DAY,
    FAULT_DETECTION_RECALL,
    LIKELY_CAUSE_TOP1,
    LINK_PRECISION_COMPOSITE,
    LINK_PRECISION_EXACT_BRIDGE,
    LINK_RECALL_COMPOSITE,
    LINK_RECALL_EXACT_BRIDGE,
    MANUAL_HOP_PRECISION,
    MANUAL_HOP_RECALL,
    TIME_TO_DETECT_P95_SECONDS,
    TRANSACTION_PAIRWISE_F1,
    VISIBILITY_GAP_ATTRIBUTION,
)
from carto_simulator import api
from carto_simulator.ground_truth import (
    EVENTS_FILE,
    GROUND_TRUTH_DIR,
    MANIFEST_FILE,
    GroundTruth,
    Manifest,
    iter_events,
)

EXACT_AND_BRIDGE: frozenset[str] = frozenset({"exact", "bridge"})
COMPOSITE: frozenset[str] = frozenset({"composite"})

REQUEST_PARAMETERS: tuple[str, ...] = ("scenario", "days", "seed", "daily_volume")
"""Generation parameters ``carto-eval`` can set; a ground truth must match them to be scored."""
SIMULATOR_OPTIONS: tuple[str, ...] = ("start_date", "faults", "noise_rate", "pii_density")
"""Simulator-only options; a ground truth generated with other values is scored as it is."""


@dataclass(frozen=True, slots=True)
class TruthEvents:
    """What the harness keeps from ``event_txn.ndjson``: the membership and two counts."""

    membership: Mapping[str, str | None]
    records: int
    transactions: int


def load_truth_events(sim_out: Path) -> TruthEvents:
    """Stream the event file once; keys map to their true transaction (``None`` for noise)."""
    membership: dict[str, str | None] = {}
    for event in iter_events(sim_out):
        membership[event.key] = event.txn_id
    transactions = {txn_id for txn_id in membership.values() if txn_id is not None}
    return TruthEvents(
        membership=membership, records=len(membership), transactions=len(transactions)
    )


def check_complete(truth: GroundTruth, events: TruthEvents) -> None:
    """Raise ``ValueError`` unless the event stream holds every record the manifest counts.

    The simulator writes the manifest first and the event stream last, so an interrupted or
    partially copied run keeps a valid manifest; the per-source counts in it add up to the line
    count of ``event_txn.ndjson``.
    """
    expected = sum(truth.manifest.counts.get(source.source_id, 0) for source in truth.sources)
    if events.records != expected:
        msg = f"{EVENTS_FILE} holds {events.records} records but the manifest counts {expected}"
        raise ValueError(msg)


def describe_error(exc: Exception) -> str:
    """One line for a CLI message: pydantic's first problem, or the exception's first line."""
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        place = f"{exc.title}: {location}" if location else exc.title
        return f"{place}: {error['msg']}"
    lines = str(exc).splitlines()
    return lines[0] if lines else type(exc).__name__


@dataclass(frozen=True, slots=True)
class Scorecard:
    """Every metric result, with the raw counts behind each value."""

    links_exact_bridge: PrecisionRecall
    links_composite: PrecisionRecall
    purity: PurityResult
    pairwise: PairwiseResult
    batches: PrecisionRecall
    faults: FaultDetectionResult
    visibility_gap_attribution: float | None
    false_alerts: FalseAlertResult
    manual_hops: PrecisionRecall
    likely_cause: CauseResult

    def values(self) -> dict[str, float | None]:
        """Metric id (spec 18.4 table) to value, ``None`` where undefined."""
        return {
            LINK_PRECISION_EXACT_BRIDGE: self.links_exact_bridge.precision,
            LINK_RECALL_EXACT_BRIDGE: self.links_exact_bridge.recall,
            LINK_PRECISION_COMPOSITE: self.links_composite.precision,
            LINK_RECALL_COMPOSITE: self.links_composite.recall,
            ENTITY_PURITY: self.purity.purity,
            TRANSACTION_PAIRWISE_F1: self.pairwise.f1,
            BATCH_KEY_PRECISION: self.batches.precision,
            BATCH_KEY_RECALL: self.batches.recall,
            FAULT_DETECTION_RECALL: self.faults.recall,
            TIME_TO_DETECT_P95_SECONDS: self.faults.time_to_detect_p95_seconds,
            FALSE_ALERTS_PER_FLOW_DAY: self.false_alerts.per_flow_day,
            VISIBILITY_GAP_ATTRIBUTION: self.visibility_gap_attribution,
            MANUAL_HOP_PRECISION: self.manual_hops.precision,
            MANUAL_HOP_RECALL: self.manual_hops.recall,
            LIKELY_CAUSE_TOP1: self.likely_cause.accuracy,
        }

    def nothing_to_score(self) -> dict[str, bool]:
        """Per core metric, whether the truth holds nothing of that kind (so n/a is correct)."""
        no_links = self.links_exact_bridge.expected == 0
        no_composites = self.links_composite.expected == 0
        no_batches = self.batches.expected == 0
        return {
            TRANSACTION_PAIRWISE_F1: self.pairwise.true_pairs == 0,
            ENTITY_PURITY: self.purity.members == 0,
            LINK_PRECISION_EXACT_BRIDGE: no_links,
            LINK_RECALL_EXACT_BRIDGE: no_links,
            LINK_PRECISION_COMPOSITE: no_composites,
            LINK_RECALL_COMPOSITE: no_composites,
            BATCH_KEY_PRECISION: no_batches,
            BATCH_KEY_RECALL: no_batches,
            FAULT_DETECTION_RECALL: self.faults.expected == 0,
        }


def resolve_alert_transactions(
    predictions: Predictions, truth_membership: Mapping[str, str | None]
) -> list[PredictedAlert]:
    """Alerts with ``affected_txn_ids`` translated from the engine's ids to true transaction ids.

    The engine names transactions by its own ids (ULIDs, spec 9.7); the truth by ``txn_000001``.
    ``txn_membership.ndjson`` ties the two: an engine transaction resolves to the true
    transactions of its member records (several when the engine over-merged). An id that no
    membership record carries is kept as it is, so true ids pass through unchanged and a run
    without a membership file is compared literally.
    """
    engine_to_truth: dict[str, set[str]] = {}
    for key, engine_txn in predictions.membership.items():
        if engine_txn is None:
            continue
        true_txn = truth_membership.get(key)
        if true_txn is not None:
            engine_to_truth.setdefault(engine_txn, set()).add(true_txn)
    resolved: list[PredictedAlert] = []
    for alert in predictions.alerts:
        affected: set[str] = set()
        for txn_id in alert.affected_txn_ids:
            affected.update(engine_to_truth.get(txn_id, {txn_id}))
        resolved.append(alert.model_copy(update={"affected_txn_ids": sorted(affected)}))
    return resolved


def score(
    truth: GroundTruth, membership: Mapping[str, str | None], predictions: Predictions
) -> Scorecard:
    """Compute every spec 18.4 metric for ``predictions`` against ``truth``.

    ``membership`` is the true transaction per locator key (:func:`load_truth_events`). Alert
    transaction ids are resolved through the predicted membership first
    (:func:`resolve_alert_transactions`). Flows for the false-alert rate are the truth's
    entities; its run window (start date and day count) comes from the manifest.
    """
    alerts = resolve_alert_transactions(predictions, membership)
    faults = metrics.fault_detection(truth.faults, alerts)
    return Scorecard(
        links_exact_bridge=metrics.link_precision_recall(
            truth.links, predictions.links, EXACT_AND_BRIDGE
        ),
        links_composite=metrics.link_precision_recall(truth.links, predictions.links, COMPOSITE),
        purity=metrics.entity_purity(truth.entities, predictions.entities),
        pairwise=metrics.transaction_pairwise_f1(membership, predictions.membership),
        batches=metrics.batch_key_precision_recall(truth.batches, predictions.batches),
        faults=faults,
        visibility_gap_attribution=metrics.visibility_gap_attribution(truth.faults, alerts, faults),
        false_alerts=metrics.false_alerts_per_flow_day(
            truth.faults,
            alerts,
            faults,
            start_date=truth.manifest.start_date,
            days=truth.manifest.days,
            n_entities=len(truth.entities),
        ),
        manual_hops=metrics.manual_hop_precision_recall(truth.manual_hops, predictions.manual_hops),
        likely_cause=metrics.likely_cause_top1(faults.faults, alerts),
    )


def report_counts(
    truth: GroundTruth, events: TruthEvents, predictions: Predictions
) -> ReportCounts:
    """The counts block of the report."""
    return ReportCounts(
        events=events.records,
        transactions=events.transactions,
        links=len(truth.links),
        entities=len(truth.entities),
        batches=len(truth.batches),
        faults=len(truth.faults),
        alerts=len(predictions.alerts),
    )


def self_check_problems(report: EvalReport, scorecard: Scorecard) -> list[str]:
    """Why a run of the truth against itself is not perfect; empty when it is.

    Perfect means no metric fails its target, and transaction F1, entity purity, link precision
    and recall (exact and bridge, and composite), batch precision and recall and fault recall
    are exactly 1.0. One of those may read n/a only when the truth holds nothing of that kind
    (a two-day scenario has no injected fault, so fault recall cannot be measured).
    """
    problems = [
        f"{metric_id} fails its target ({result.label}: {result.value})"
        for metric_id, result in report.metrics.items()
        if result.status == "fail"
    ]
    for metric_id, nothing in scorecard.nothing_to_score().items():
        value = report.metrics[metric_id].value
        if value == 1.0 or (value is None and nothing):
            continue
        shown = "n/a" if value is None else f"{value}"
        problems.append(f"{metric_id} is {shown}, expected exactly 1.0")
    return problems


def ensure_ground_truth(
    request: api.GenerationRequest, sim_out: Path, *, regenerate: bool = False
) -> str | None:
    """Generate ``request`` into ``sim_out`` when no ground truth is there or ``regenerate`` is set.

    Returns the reason generation ran, or None when the existing ground truth was kept. Nothing
    else ever replaces an existing ground truth, because generating empties ``sim_out``: one
    whose manifest differs from the request on a parameter ``carto-eval`` can set
    (:data:`REQUEST_PARAMETERS`) is refused with ``ValueError`` naming the difference, and so is
    one whose manifest cannot be read. The simulator refuses to empty a populated directory that
    is not a previous run (``ValueError`` too).
    """
    manifest_path = sim_out / GROUND_TRUTH_DIR / MANIFEST_FILE
    if regenerate:
        reason = "--regenerate"
    elif not manifest_path.is_file():
        reason = f"no ground truth under {sim_out}"
    else:
        mismatch = parameter_differences(_read_manifest(manifest_path), request, REQUEST_PARAMETERS)
        if mismatch:
            msg = (
                f"existing ground truth under {sim_out} was generated with {mismatch}; "
                "pass --regenerate to replace it or the matching parameters to score it"
            )
            raise ValueError(msg)
        return None
    api.generate(request, sim_out)
    return reason


def _read_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        msg = (
            f"ground truth manifest {path} is unreadable ({describe_error(exc)}); "
            "rerun with --regenerate"
        )
        raise ValueError(msg) from exc


def parameter_differences(
    manifest: Manifest, request: api.GenerationRequest, names: Iterable[str]
) -> str:
    """``name=value`` for each parameter in ``names`` where the manifest differs from the request.

    Comma separated, in the order of ``names``; empty when they agree.
    """
    actual = {
        "scenario": manifest.scenario,
        "days": manifest.days,
        "seed": manifest.seed,
        "daily_volume": manifest.daily_volume,
        "start_date": manifest.start_date,
        "faults": manifest.faults_enabled,
        "noise_rate": manifest.noise_rate,
        "pii_density": manifest.pii_density,
    }
    wanted = {
        "scenario": request.scenario,
        "days": request.days,
        "seed": request.seed,
        "daily_volume": request.daily_volume,
        "start_date": request.start_date,
        "faults": request.faults,
        "noise_rate": request.noise_rate,
        "pii_density": request.pii_density,
    }
    return ", ".join(f"{name}={actual[name]}" for name in names if actual[name] != wanted[name])
