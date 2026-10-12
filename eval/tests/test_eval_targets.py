"""The spec 18.4 table as data: rows, evaluation, tolerances and rendering."""

from __future__ import annotations

import pytest

from carto_eval import targets as t


def test_table_matches_the_spec_rows() -> None:
    ids = [row.metric_id for row in t.TARGETS]
    assert len(ids) == len(set(ids)) == 15
    assert set(t.TARGETS_BY_ID) == set(ids)
    expected = {
        t.LINK_PRECISION_EXACT_BRIDGE: (0.95, ">=", "ratio"),
        t.LINK_RECALL_EXACT_BRIDGE: (0.90, ">=", "ratio"),
        t.LINK_PRECISION_COMPOSITE: (0.85, ">=", "ratio"),
        t.LINK_RECALL_COMPOSITE: (0.70, ">=", "ratio"),
        t.ENTITY_PURITY: (0.95, ">=", "ratio"),
        t.TRANSACTION_PAIRWISE_F1: (0.95, ">=", "ratio"),
        t.BATCH_KEY_PRECISION: (0.95, ">=", "ratio"),
        t.BATCH_KEY_RECALL: (0.90, ">=", "ratio"),
        t.FAULT_DETECTION_RECALL: (1.0, ">=", "ratio"),
        t.TIME_TO_DETECT_P95_SECONDS: (120.0, "<=", "seconds"),
        t.FALSE_ALERTS_PER_FLOW_DAY: (1.0, "<=", "per_flow_day"),
        t.VISIBILITY_GAP_ATTRIBUTION: (1.0, ">=", "ratio"),
        t.MANUAL_HOP_PRECISION: (0.80, ">=", "ratio"),
        t.MANUAL_HOP_RECALL: (0.80, ">=", "ratio"),
        t.LIKELY_CAUSE_TOP1: (0.70, ">=", "ratio"),
    }
    actual = {row.metric_id: (row.target, row.comparison, row.unit) for row in t.TARGETS}
    assert actual == expected
    assert all(row.label for row in t.TARGETS)


def test_evaluate_handles_none_and_both_directions() -> None:
    at_least = t.TARGETS_BY_ID[t.TRANSACTION_PAIRWISE_F1]
    assert at_least.evaluate(None) == "n/a"
    assert at_least.evaluate(0.95) == "pass"
    assert at_least.evaluate(0.9499) == "fail"
    assert at_least.evaluate(1.0) == "pass"
    at_most = t.TARGETS_BY_ID[t.TIME_TO_DETECT_P95_SECONDS]
    assert at_most.evaluate(120.0) == "pass"
    assert at_most.evaluate(120.5) == "fail"
    assert at_most.evaluate(0.0) == "pass"
    assert at_most.evaluate(None) == "n/a"


def test_tolerance_and_worsening() -> None:
    ratio_row = t.TARGETS_BY_ID[t.ENTITY_PURITY]
    assert ratio_row.tolerance() == pytest.approx(0.02)
    assert ratio_row.tolerance(points=5) == pytest.approx(0.05)
    assert ratio_row.worsening(0.97, 0.93) == pytest.approx(0.04)
    assert ratio_row.worsening(0.93, 0.97) == pytest.approx(-0.04)
    seconds_row = t.TARGETS_BY_ID[t.TIME_TO_DETECT_P95_SECONDS]
    assert seconds_row.tolerance() == 1.0
    assert seconds_row.tolerance(points=5, unit_tolerance=3) == 3.0
    assert seconds_row.worsening(100.0, 101.5) == 1.5
    per_day = t.TARGETS_BY_ID[t.FALSE_ALERTS_PER_FLOW_DAY]
    assert per_day.worsening(0.5, 0.25) == -0.25


def test_formatting() -> None:
    ratio_row = t.TARGETS_BY_ID[t.LINK_PRECISION_EXACT_BRIDGE]
    assert ratio_row.format_value(None) == "n/a"
    assert ratio_row.format_value(0.95) == "0.9500"
    assert ratio_row.format_target() == ">= 0.95"
    seconds_row = t.TARGETS_BY_ID[t.TIME_TO_DETECT_P95_SECONDS]
    assert seconds_row.format_value(12) == "12.0 s"
    assert seconds_row.format_target() == "<= 120 s"
    per_day = t.TARGETS_BY_ID[t.FALSE_ALERTS_PER_FLOW_DAY]
    assert per_day.format_value(0.5) == "0.50"
    assert per_day.format_target() == "<= 1.00"
