"""Report rendering (JSON and Markdown), history and regression detection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carto_eval import report as r
from carto_eval import targets as t
from carto_eval.report import EvalReport, HistoryRecord, ReportCounts

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
COUNTS = ReportCounts(
    events=4690, transactions=60, links=10, entities=3, batches=4, faults=1, alerts=0
)


def perfect_values() -> dict[str, float | None]:
    values: dict[str, float | None] = dict.fromkeys(t.TARGETS_BY_ID, 1.0)
    values[t.TIME_TO_DETECT_P95_SECONDS] = 30.0
    values[t.FALSE_ALERTS_PER_FLOW_DAY] = 0.0
    return values


def make_report(
    values: Mapping[str, float | None],
    *,
    present: bool = True,
    scenario: str = "shop",
    days: int = 2,
) -> EvalReport:
    return r.build_report(
        scenario=scenario,
        seed=1,
        days=days,
        generated_at=NOW,
        predictions_present=present,
        counts=COUNTS,
        values=values,
    )


def test_markdown_has_heading_predictions_line_and_one_table() -> None:
    values = perfect_values()
    values[t.TRANSACTION_PAIRWISE_F1] = 0.9
    values[t.LIKELY_CAUSE_TOP1] = None
    text = make_report(values).to_markdown()
    lines = text.splitlines()
    assert lines[0] == "# carto eval: shop"
    assert "Scenario `shop`, seed 1, 2 days, generated at 2026-10-06T18:00:00Z." in lines
    assert "Predictions: present (engine output scored)." in lines
    counts = "Counts: 4,690 events, 60 transactions, 10 links, 3 entities, 4 batches, 1 faults, "
    assert counts + "0 alerts." in lines
    assert text.count("| Metric | Value | Target | Status |") == 1
    assert "| Transaction pairwise F1 | 0.9000 | >= 0.95 | fail |" in lines
    assert "| Likely cause top-1 accuracy on injected faults | n/a | >= 0.70 | n/a |" in lines
    assert "| Time to detect after deadline (p95) | 30.0 s | <= 120 s | pass |" in lines
    assert "| False alerts on fault-free days (per flow per day) | 0.00 | <= 1.00 | pass |" in lines
    assert lines[-1] == "Regressions against the previous run of this scenario: none."
    assert text.endswith("\n")
    absent = make_report(values, present=False).to_markdown()
    assert "Predictions: not present (the empty prediction scored;" in absent


def test_json_is_sorted_indented_and_lf(tmp_path: Path) -> None:
    report = make_report(perfect_values()).with_regressions(["entity_purity: 1.0000 -> n/a"])
    path = tmp_path / "shop.json"
    report.write_json(path)
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b'{\n  "counts": {\n    "alerts": 0,')
    payload = json.loads(raw)
    assert list(payload) == sorted(payload)
    assert payload["generated_at"] == "2026-10-06T18:00:00Z"
    assert payload["regressions"] == ["entity_purity: 1.0000 -> n/a"]
    assert payload["metrics"][t.ENTITY_PURITY] == {
        "label": "Entity family purity",
        "value": 1.0,
        "target": 0.95,
        "comparison": ">=",
        "unit": "ratio",
        "status": "pass",
    }
    assert EvalReport.model_validate(payload) == report
    markdown = tmp_path / "shop.md"
    report.write_markdown(markdown)
    assert b"\r\n" not in markdown.read_bytes()
    assert "- entity_purity: 1.0000 -> n/a" in markdown.read_text(encoding="utf-8")


def test_build_report_rejects_unknown_metrics_and_fills_missing_ones() -> None:
    with pytest.raises(KeyError, match=r"not in the spec 18\.4 table"):
        make_report({"made_up": 1.0})
    report = make_report({})
    assert list(report.metrics) == [row.metric_id for row in t.TARGETS]
    assert all(result.status == "n/a" for result in report.metrics.values())
    assert report.failed_metrics() == []
    values = perfect_values()
    values[t.BATCH_KEY_RECALL] = 0.5
    values[t.FALSE_ALERTS_PER_FLOW_DAY] = 2.0
    assert make_report(values).failed_metrics() == [t.BATCH_KEY_RECALL, t.FALSE_ALERTS_PER_FLOW_DAY]


def test_history_append_read_and_last_record(tmp_path: Path) -> None:
    path = tmp_path / r.HISTORY_FILE
    assert r.read_history(path) == []
    first = make_report(perfect_values()).history_record()
    other = make_report(perfect_values(), scenario="payer").history_record()
    second_values = perfect_values()
    second_values[t.ENTITY_PURITY] = 0.5
    second = make_report(second_values).history_record()
    longer = make_report(perfect_values(), days=14).history_record()
    for record in (first, other, second, longer):
        r.append_history(path, record)
    raw = path.read_bytes()
    assert raw.count(b"\n") == 4 and b"\r\n" not in raw
    records = r.read_history(path)
    assert records == [first, other, second, longer]
    # only a run of the same scenario, seed and day count is comparable
    assert r.last_record(records, "shop", 1, 2) == second
    assert r.last_record(records, "shop", 1, 14) == longer
    assert r.last_record(records, "payer", 1, 2) == other
    assert r.last_record(records, "people_ops", 1, 2) is None
    assert r.last_record(records, "shop", 2, 2) is None
    assert r.last_record(records, "shop", 1, 3) is None
    assert second.metrics[t.ENTITY_PURITY] == 0.5
    path.write_text(first.model_dump_json() + "\n\nnot a record\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"history\.ndjson: line 3"):
        r.read_history(path)


def test_find_regressions() -> None:
    previous = make_report(perfect_values()).history_record()
    assert r.find_regressions(None, make_report({})) == []
    # within tolerance: a drop of exactly two points is not a regression
    values = perfect_values()
    values[t.TRANSACTION_PAIRWISE_F1] = 0.98
    assert r.find_regressions(previous, make_report(values)) == []
    # beyond tolerance on a ratio, and a count rising by more than one unit
    values[t.TRANSACTION_PAIRWISE_F1] = 0.97
    values[t.FALSE_ALERTS_PER_FLOW_DAY] = 1.5
    values[t.TIME_TO_DETECT_P95_SECONDS] = 31.0  # exactly one unit worse: tolerated
    regressions = r.find_regressions(previous, make_report(values))
    assert regressions == [
        "transaction_pairwise_f1: 1.0000 -> 0.9700 (worse by 0.0300, tolerance 0.0200)",
        "false_alerts_per_flow_day: 0.00 -> 1.50 (worse by 1.50, tolerance 1.00)",
    ]
    # a wider tolerance silences the ratio but not the count
    assert r.find_regressions(previous, make_report(values), tolerance_points=5) == [
        "false_alerts_per_flow_day: 0.00 -> 1.50 (worse by 1.50, tolerance 1.00)"
    ]
    # improvements never count, whatever the direction of the target
    values = perfect_values()
    values[t.TIME_TO_DETECT_P95_SECONDS] = 5.0
    assert r.find_regressions(previous, make_report(values)) == []
    # defined to n/a regresses; n/a to defined does not; a metric unknown before is skipped
    values = perfect_values()
    values[t.LIKELY_CAUSE_TOP1] = None
    assert r.find_regressions(previous, make_report(values)) == [
        "likely_cause_top1: 1.0000 -> n/a (no longer measurable)"
    ]
    sparse = HistoryRecord(
        scenario="shop",
        seed=1,
        days=2,
        generated_at=NOW,
        predictions_present=False,
        metrics={t.LIKELY_CAUSE_TOP1: None},
    )
    assert r.find_regressions(sparse, make_report(perfect_values())) == []
