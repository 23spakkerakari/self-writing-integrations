"""Prediction models: file formats, loading errors, the empty and the perfect prediction."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from carto_eval import predictions as p
from carto_eval.predictions import Predictions, PredictionsError
from carto_simulator.ground_truth import (
    ActorKind,
    BatchTruth,
    EntityField,
    EntityTruth,
    EventTruth,
    FaultKind,
    FaultTruth,
    FieldRef,
    GroundTruth,
    LinkTruth,
    Manifest,
    ManualHopTruth,
    MarkerSet,
)

T0 = datetime(2026, 9, 28, 14, 20, tzinfo=UTC)
CART = FieldRef(system_id="sys_webstore", source_id="src_webstore_log", field="cart_id")
ORDER_CART = FieldRef(system_id="sys_orders", source_id="src_orders_log", field="cart_id")
FILE_NAME = FieldRef(system_id="sys_shipping", source_id="src_ship_sftp", field="file_name")


def sample_predictions() -> Predictions:
    return Predictions(
        links=[
            p.PredictedLink(
                a=CART,
                form_a="raw",
                b=ORDER_CART,
                form_b="raw",
                link_type="exact",
                role="transaction",
                score=0.97,
                rank=1,
            )
        ],
        entities=[
            p.PredictedEntity(
                entity_id="fam_1",
                fields=[EntityField(ref=CART, form="raw"), EntityField(ref=ORDER_CART, form="raw")],
            )
        ],
        membership={"src_webstore_log:app-2026-09-23.ndjson:line:3": "01J9", "k2": None},
        batches=[p.PredictedBatch(key_value="SHIP_20260923_2113.csv", txn_ids=["01J9"])],
        alerts=[
            p.PredictedAlert(
                alert_id="alert_1",
                expectation_kind="error_rate",
                target="payments:authorized",
                opened_at=T0,
                resolved_at=T0.replace(hour=15),
                affected_txn_ids=["01J9"],
                likely_causes=["error_spike"],
                system_id="sys_payments",
            )
        ],
        manual_hops=[
            p.PredictedManualHop(
                from_node="payments:authorized", to_node="warehouse:po_created", score=0.82
            )
        ],
        present=True,
    )


def test_empty_prediction() -> None:
    empty = Predictions.empty()
    assert not empty.present
    assert empty.links == [] and empty.entities == [] and empty.membership == {}
    assert empty.batches == [] and empty.alerts == [] and empty.manual_hops == []


def test_load_missing_directory_is_the_empty_prediction(tmp_path: Path) -> None:
    assert Predictions.load(tmp_path / "nowhere") == Predictions.empty()


def test_load_partial_directory_leaves_missing_parts_empty(tmp_path: Path) -> None:
    sample = sample_predictions()
    sample.write(tmp_path)
    for name in p.PREDICTION_FILES:
        if name != p.LINKS_FILE:
            (tmp_path / name).unlink()
    loaded = Predictions.load(tmp_path)
    assert loaded.present
    assert loaded.links == sample.links
    assert loaded.membership == {} and loaded.alerts == []


def test_write_then_load_round_trip(tmp_path: Path) -> None:
    sample = sample_predictions()
    sample.write(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(p.PREDICTION_FILES)
    assert b"\r\n" not in (tmp_path / p.MEMBERSHIP_FILE).read_bytes()
    assert (tmp_path / p.MEMBERSHIP_FILE).read_text(encoding="utf-8").splitlines() == [
        '{"key":"src_webstore_log:app-2026-09-23.ndjson:line:3","txn_id":"01J9"}',
        '{"key":"k2","txn_id":null}',
    ]
    assert Predictions.load(tmp_path) == sample


def test_malformed_json_names_the_file_and_the_place(tmp_path: Path) -> None:
    (tmp_path / p.LINKS_FILE).write_text("[{", encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"links\.json: line 1 column 3"):
        Predictions.load(tmp_path)


def test_wrong_shape_and_invalid_item_are_reported(tmp_path: Path) -> None:
    (tmp_path / p.ENTITIES_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"entities\.json: expected a JSON array"):
        Predictions.load(tmp_path)
    (tmp_path / p.ENTITIES_FILE).unlink()
    (tmp_path / p.BATCHES_FILE).write_text(
        '[{"key_value": "x"}, {"key_value": "y", "txn_ids": "not-a-list"}]', encoding="utf-8"
    )
    with pytest.raises(PredictionsError, match=r"batches\.json: item 1: txn_ids"):
        Predictions.load(tmp_path)
    (tmp_path / p.BATCHES_FILE).unlink()
    (tmp_path / p.LINKS_FILE).write_text('[{"unknown": 1}]', encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"links\.json: item 0"):
        Predictions.load(tmp_path)


def test_membership_line_errors(tmp_path: Path) -> None:
    good = '{"key": "a", "txn_id": "t1"}\n'
    (tmp_path / p.MEMBERSHIP_FILE).write_text(good + '{"key": "b"}\n', encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"txn_membership\.ndjson: line 2: txn_id"):
        Predictions.load(tmp_path)
    (tmp_path / p.MEMBERSHIP_FILE).write_text(good + "\n" + good, encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"line 3: duplicate key 'a'"):
        Predictions.load(tmp_path)
    (tmp_path / p.MEMBERSHIP_FILE).write_text(good + "not json\n", encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"line 2"):
        Predictions.load(tmp_path)


def test_rank_is_any_integer(tmp_path: Path) -> None:
    # the contract is ``rank: int | None = None``: a queue numbered from 0 is scored too
    sample = sample_predictions()
    zero = sample.links[0].model_copy(update={"rank": 0})
    assert p.PredictedLink.model_validate(zero.model_dump(mode="json")).rank == 0
    sample.model_copy(update={"links": [zero]}).write(tmp_path)
    assert Predictions.load(tmp_path).links[0].rank == 0


def test_duplicate_alert_ids_are_rejected(tmp_path: Path) -> None:
    # the fault metrics key alerts by id, so a repeat would corrupt the false-alert count
    first = sample_predictions().alerts[0]
    second = first.model_copy(update={"expectation_kind": "volume", "affected_txn_ids": ["01J8"]})
    Predictions(alerts=[first, second], present=True).write(tmp_path)
    with pytest.raises(
        PredictionsError, match=r"alerts\.json: item 1: duplicate alert_id 'alert_1'"
    ):
        Predictions.load(tmp_path)


def test_non_utf8_files_are_reported(tmp_path: Path) -> None:
    latin1 = '{"key": "src_x:café.log:line:1", "txn_id": "E1"}\n'.encode("latin-1")
    (tmp_path / p.MEMBERSHIP_FILE).write_bytes(latin1)
    with pytest.raises(PredictionsError, match=r"txn_membership\.ndjson: not UTF-8"):
        Predictions.load(tmp_path)
    (tmp_path / p.MEMBERSHIP_FILE).unlink()
    (tmp_path / p.LINKS_FILE).write_bytes(b'[{"form_a": "caf\xe9"}]')
    with pytest.raises(PredictionsError, match=r"links\.json: not UTF-8"):
        Predictions.load(tmp_path)


def test_deeply_nested_json_is_reported(tmp_path: Path) -> None:
    (tmp_path / p.LINKS_FILE).write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(PredictionsError, match=r"links\.json: JSON nested too deeply"):
        Predictions.load(tmp_path)


def test_models_reject_extras_naive_times_and_bad_scores() -> None:
    with pytest.raises(ValidationError):
        p.PredictedAlert(
            alert_id="a", expectation_kind="schedule", target="t", opened_at=T0.replace(tzinfo=None)
        )
    with pytest.raises(ValidationError):
        p.PredictedManualHop(from_node="a", to_node="b", score=1.5)
    with pytest.raises(ValidationError):
        p.PredictedBatch(key_value="x", extra="no")  # type: ignore[call-arg]
    # a non-UTC offset is normalized to UTC
    local = p.PredictedAlert(
        alert_id="a",
        expectation_kind="schedule",
        target="t",
        opened_at=datetime.fromisoformat("2026-09-28T10:20:00-04:00"),
    )
    assert local.opened_at == T0 and local.opened_at.tzinfo is UTC


def minimal_truth() -> GroundTruth:
    spike = FaultTruth(
        fault_id="f1",
        kind=FaultKind.ERROR_SPIKE,
        start=T0,
        end=T0.replace(hour=15),
        system_id="sys_payments",
        source_id="src_payments_xml",
        affected_txn_ids=["txn_000001"],
        expected_alert_kind="error_rate",
        expected_cause_kind="error_spike",
    )
    gap = FaultTruth(
        fault_id="f3",
        kind=FaultKind.VISIBILITY_GAP,
        start=T0,
        end=None,
        system_id="sys_webstore",
        affected_txn_ids=["txn_000002"],
    )
    skew = FaultTruth(
        fault_id="f6",
        kind=FaultKind.CLOCK_SKEW,
        start=T0,
        end=None,
        system_id="sys_warehouse",
        expected_alert=False,
    )
    return GroundTruth(
        manifest=Manifest(
            generator_version="test",
            scenario="unit",
            seed=1,
            days=1,
            start_date=date(2026, 9, 28),
            daily_volume=2,
            faults_enabled=True,
            noise_rate=1.0,
            pii_density=0.0,
            counts={},
        ),
        sources=[],
        links=[
            LinkTruth(
                link_id="L01",
                a=CART,
                form_a="raw",
                b=ORDER_CART,
                form_b="raw",
                link_type="exact",
                role="transaction",
                direction="a_to_b",
                entity_id="ent_order",
            )
        ],
        entities=[
            EntityTruth(
                entity_id="ent_order",
                name="Order",
                fields=[EntityField(ref=CART, form="raw"), EntityField(ref=ORDER_CART, form="raw")],
            )
        ],
        batches=[
            BatchTruth(
                batch_id="batch_SHIP_1",
                key_field=FILE_NAME,
                key_value="SHIP_1.csv",
                txn_ids=["txn_000001"],
            )
        ],
        faults=[spike, gap, skew],
        manual_hops=[
            ManualHopTruth(
                hop_id="hop_manual",
                from_node="payments:authorized",
                to_node="warehouse:po_created",
                entity_id="ent_order",
                manual=True,
                share_manual=0.3,
                actors=["jsmith"],
                typo_rate=0.02,
            ),
            ManualHopTruth(
                hop_id="hop_auto",
                from_node="webstore:checkout_completed",
                to_node="orders:order_created",
                entity_id="ent_order",
                manual=False,
                share_manual=0.0,
            ),
        ],
        markers=MarkerSet(marker_tokens=[], pii_values=[], identifier_values={}),
    )


def test_from_truth_is_the_perfect_prediction() -> None:
    events = [
        EventTruth(
            key="src_webstore_log:app.ndjson:line:1",
            source_id="src_webstore_log",
            system_id="sys_webstore",
            node="webstore:cart_created",
            observed_at=T0,
            txn_id="txn_000001",
            actor_kind=ActorKind.SERVICE,
        ),
        EventTruth(
            key="src_webstore_log:app.ndjson:line:2",
            source_id="src_webstore_log",
            system_id="sys_webstore",
            node="webstore:noise",
            observed_at=T0,
            txn_id=None,
        ),
    ]
    perfect = Predictions.from_truth(minimal_truth(), iter(events))
    assert perfect.present
    (link,) = perfect.links
    assert (link.a, link.b, link.link_type, link.role, link.score) == (
        CART,
        ORDER_CART,
        "exact",
        "transaction",
        1.0,
    )
    assert [e.entity_id for e in perfect.entities] == ["ent_order"]
    assert perfect.membership == {
        "src_webstore_log:app.ndjson:line:1": "txn_000001",
        "src_webstore_log:app.ndjson:line:2": None,
    }
    assert [(b.key_value, b.txn_ids) for b in perfect.batches] == [("SHIP_1.csv", ["txn_000001"])]
    # one alert per fault that expects one; the clock skew gets none
    spike_alert, gap_alert = perfect.alerts
    assert spike_alert.alert_id == "alert_f1"
    assert spike_alert.expectation_kind == "error_rate"
    assert spike_alert.target == "src_payments_xml"
    assert (spike_alert.opened_at, spike_alert.resolved_at) == (T0, T0.replace(hour=15))
    assert spike_alert.affected_txn_ids == ["txn_000001"]
    assert spike_alert.likely_causes == ["error_spike"]
    assert spike_alert.system_id == "sys_payments"
    assert not spike_alert.is_visibility_gap
    # without an expected alert kind the fault kind stands in; no expected cause, no causes
    assert gap_alert.expectation_kind == "visibility_gap"
    assert gap_alert.is_visibility_gap
    assert gap_alert.likely_causes == []
    assert gap_alert.target == "sys_webstore"
    assert [(h.score, h.confirmed) for h in perfect.manual_hops] == [(1.0, True), (0.0, False)]
