"""Scoring glue: alert id resolution, the scorecard and the self-check rule."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from carto_eval import scoring
from carto_eval import targets as t
from carto_eval.metrics import FalseAlertResult, PrecisionRecall
from carto_eval.predictions import PredictedAlert, Predictions
from carto_eval.report import ReportCounts, build_report
from carto_simulator import api
from carto_simulator.ground_truth import (
    EntityField,
    EntityTruth,
    FaultKind,
    FaultTruth,
    FieldRef,
    GroundTruth,
    Manifest,
    MarkerSet,
    SourceDef,
    SourceFormat,
    SourceKind,
)

T0 = datetime(2026, 9, 28, 14, 20, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
CART = FieldRef(system_id="sys_webstore", source_id="src_webstore_log", field="cart_id")


def alert(
    alert_id: str, *affected: str, kind: str = "error_rate", opened: datetime = T0
) -> PredictedAlert:
    return PredictedAlert(
        alert_id=alert_id,
        expectation_kind=kind,
        target="payments:authorized",
        opened_at=opened,
        affected_txn_ids=list(affected),
        likely_causes=["error_spike"],
    )


def test_resolve_alert_transactions_through_the_membership() -> None:
    # engine transaction E1 holds keys k1 and k2, which truly belong to txn_000001 and
    # txn_000002 (an over-merge); E2 holds k3 (txn_000003); E3 holds only a noise key
    truth_membership = {"k1": "txn_000001", "k2": "txn_000002", "k3": "txn_000003", "n": None}
    predictions = Predictions(
        membership={"k1": "E1", "k2": "E1", "k3": "E2", "n": "E3"},
        alerts=[alert("a1", "E1"), alert("a2", "E2", "E3", "txn_000009"), alert("a3")],
        present=True,
    )
    resolved = scoring.resolve_alert_transactions(predictions, truth_membership)
    assert [a.affected_txn_ids for a in resolved] == [
        ["txn_000001", "txn_000002"],
        ["E3", "txn_000003", "txn_000009"],  # E3 and a true id are unknown: kept as they are
        [],
    ]
    assert [a.alert_id for a in resolved] == ["a1", "a2", "a3"]
    assert resolved[0].likely_causes == ["error_spike"]


def tiny_truth() -> GroundTruth:
    spike = FaultTruth(
        fault_id="f1",
        kind=FaultKind.ERROR_SPIKE,
        start=T0,
        end=None,
        system_id="sys_payments",
        affected_txn_ids=["txn_000001"],
        expected_alert_kind="error_rate",
        expected_cause_kind="error_spike",
    )
    return GroundTruth(
        manifest=Manifest(
            generator_version="test",
            scenario="unit",
            seed=1,
            days=2,
            start_date=date(2026, 9, 28),
            daily_volume=1,
            faults_enabled=True,
            noise_rate=1.0,
            pii_density=0.0,
            counts={},
        ),
        sources=[],
        links=[],
        entities=[
            EntityTruth(
                entity_id="ent_order", name="Order", fields=[EntityField(ref=CART, form="raw")]
            )
        ],
        batches=[],
        faults=[spike],
        manual_hops=[],
        markers=MarkerSet(marker_tokens=[], pii_values=[], identifier_values={}),
    )


def test_score_detects_a_fault_through_engine_transaction_ids() -> None:
    truth = tiny_truth()
    truth_membership = {"k1": "txn_000001", "k2": "txn_000001", "k3": "txn_000002"}
    engine = Predictions(
        membership={"k1": "E1", "k2": "E1", "k3": "E2"},
        alerts=[alert("a1", "E1"), alert("a_noise", "E2", kind="volume")],
        present=True,
    )
    card = scoring.score(truth, truth_membership, engine)
    assert card.faults.recall == 1.0
    assert card.faults.faults[0].matched_alert_id == "a1"
    assert card.faults.false_alert_ids == ("a_noise",)
    # the open-ended fault starts on day 1 and runs into day 2 of the two-day manifest, so no
    # run day is fault-free: the false alert counts in the total only and the rate is undefined
    assert card.false_alerts == FalseAlertResult(
        false_alerts_total=1,
        false_alerts_on_fault_free_days=0,
        fault_free_days=0,
        run_days=2,
        n_entities=1,
    )
    assert card.likely_cause.accuracy == 1.0
    assert card.pairwise.f1 == 1.0
    assert card.purity.purity is None
    values = card.values()
    assert set(values) == set(t.TARGETS_BY_ID)
    assert values[t.FAULT_DETECTION_RECALL] == 1.0
    assert values[t.TIME_TO_DETECT_P95_SECONDS] == 0.0
    assert values[t.FALSE_ALERTS_PER_FLOW_DAY] is None


def test_score_rates_false_alerts_on_the_fault_free_days_of_the_manifest_window() -> None:
    # three days from the manifest's start date (Sep 28 to 30): the open-ended fault covers
    # Sep 28 and 29, so Sep 30 is the one fault-free day. Two volume alerts match no expected
    # fault; the one on Sep 30 is rated, the one on Sep 28 only counted: 1 over one flow times
    # one day
    truth = tiny_truth()
    truth = truth.model_copy(update={"manifest": truth.manifest.model_copy(update={"days": 3})})
    on_faulty_day = alert("a_noise_faulty", kind="volume")
    on_fault_free_day = alert("a_noise_free", kind="volume", opened=T0 + timedelta(days=2))
    engine = Predictions(
        alerts=[alert("a1", "txn_000001"), on_faulty_day, on_fault_free_day], present=True
    )
    card = scoring.score(truth, {"k1": "txn_000001"}, engine)
    assert card.faults.false_alert_ids == ("a_noise_faulty", "a_noise_free")
    assert card.false_alerts == FalseAlertResult(
        false_alerts_total=2,
        false_alerts_on_fault_free_days=1,
        fault_free_days=1,
        run_days=3,
        n_entities=1,
    )
    assert card.values()[t.FALSE_ALERTS_PER_FLOW_DAY] == 1.0


def test_self_check_problems() -> None:
    truth = tiny_truth()
    truth_membership = {"k1": "txn_000001", "k2": "txn_000001"}
    counts = ReportCounts(
        events=2, transactions=1, links=0, entities=1, batches=0, faults=1, alerts=1
    )

    def report_for(predictions: Predictions) -> list[str]:
        card = scoring.score(truth, truth_membership, predictions)
        report = build_report(
            scenario="unit",
            seed=1,
            days=2,
            generated_at=NOW,
            predictions_present=True,
            counts=counts,
            values=card.values(),
        )
        return scoring.self_check_problems(report, card)

    perfect = Predictions(
        membership=dict(truth_membership),
        entities=[],
        alerts=[alert("a1", "txn_000001")],
        present=True,
    )
    # no links and no batches in the truth: their n/a is fine; purity n/a is fine without families
    assert report_for(perfect) == []
    # a missed fault fails the recall target and the exactly-1.0 rule
    missed = perfect.model_copy(update={"alerts": []})
    problems = report_for(missed)
    assert any(p.startswith("fault_detection_recall fails its target") for p in problems)
    assert "fault_detection_recall is 0.0, expected exactly 1.0" in problems
    # an unassigned record leaves pairwise f1 below 1.0 without any target failing elsewhere
    half = perfect.model_copy(update={"membership": {"k1": "E1", "k2": None}})
    problems = report_for(half)
    assert "transaction_pairwise_f1 is 0.0, expected exactly 1.0" in problems


def test_self_check_requires_perfect_composite_links() -> None:
    truth = tiny_truth()
    truth_membership = {"k1": "txn_000001", "k2": "txn_000001"}
    perfect = Predictions(
        membership=dict(truth_membership), alerts=[alert("a1", "txn_000001")], present=True
    )
    card = scoring.score(truth, truth_membership, perfect)
    # three of four composite links found: recall 0.75 passes the 0.70 target but is not perfect
    card = replace(card, links_composite=PrecisionRecall(matched=3, predicted=3, expected=4))
    report = build_report(
        scenario="unit",
        seed=1,
        days=2,
        generated_at=NOW,
        predictions_present=True,
        counts=ReportCounts(
            events=2, transactions=1, links=4, entities=1, batches=0, faults=1, alerts=1
        ),
        values=card.values(),
    )
    assert report.metrics[t.LINK_RECALL_COMPOSITE].status == "pass"
    assert scoring.self_check_problems(report, card) == [
        "link_recall_composite is 0.75, expected exactly 1.0"
    ]
    # no composite link in the truth: n/a is right
    none = replace(card, links_composite=PrecisionRecall(matched=0, predicted=0, expected=0))
    report = build_report(
        scenario="unit",
        seed=1,
        days=2,
        generated_at=NOW,
        predictions_present=True,
        counts=ReportCounts(
            events=2, transactions=1, links=0, entities=1, batches=0, faults=1, alerts=1
        ),
        values=none.values(),
    )
    assert scoring.self_check_problems(report, none) == []


def test_parameter_differences_name_what_the_manifest_holds() -> None:
    manifest = tiny_truth().manifest  # unit, seed 1, days 2, volume 1, faults on, pii 0.0
    request = api.GenerationRequest(scenario="unit", days=2, seed=1, daily_volume=1)
    assert scoring.parameter_differences(manifest, request, scoring.REQUEST_PARAMETERS) == ""
    # the start date and pii_density 0.0 are not the defaults: simulator-only options, reported
    # separately and in table order
    assert (
        scoring.parameter_differences(manifest, request, scoring.SIMULATOR_OPTIONS)
        == "start_date=2026-09-28, pii_density=0.0"
    )
    other = api.GenerationRequest(scenario="unit", days=14, seed=2, daily_volume=1)
    assert (
        scoring.parameter_differences(manifest, other, scoring.REQUEST_PARAMETERS)
        == "days=2, seed=1"
    )


def test_check_complete_compares_the_event_stream_with_the_manifest_counts() -> None:
    truth = tiny_truth()
    source = SourceDef(
        source_id="src_a",
        system_id="sys_a",
        system_name="A",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.NDJSON,
        path="a.ndjson",
        timezone="UTC",
        timestamp_format="iso8601",
    )
    counts = {"src_a": 2, "transactions": 1, "events": 1, "noise": 1, "files": 1}
    counted = truth.model_copy(
        update={
            "manifest": truth.manifest.model_copy(update={"counts": counts}),
            "sources": [source],
        }
    )
    # the totals are not sources: two records counted, two read
    complete = scoring.TruthEvents(
        membership={"k1": "txn_000001", "k2": None}, records=2, transactions=1
    )
    scoring.check_complete(counted, complete)
    short = scoring.TruthEvents(membership={"k1": "txn_000001"}, records=1, transactions=1)
    with pytest.raises(
        ValueError, match=r"event_txn\.ndjson holds 1 records but the manifest counts 2"
    ):
        scoring.check_complete(counted, short)
