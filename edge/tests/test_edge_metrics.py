"""carto_edge.metrics: a registry of counters and gauges rendered in the Prometheus text format
(spec 16), with validated names, escaped label values and a cap on label combinations."""

from __future__ import annotations

import pytest

from carto_edge.metrics import (
    EDGE_SERIES,
    MAX_SERIES_PER_METRIC,
    EdgeMetrics,
    default_metrics,
)


def test_counter_and_gauge_round_trip() -> None:
    metrics = EdgeMetrics()
    metrics.counter("carto_test_total", "Things.")
    metrics.gauge("carto_test_depth", "Depth.")
    metrics.inc("carto_test_total")
    metrics.inc("carto_test_total", 2, source_id="src_a")
    metrics.set("carto_test_depth", 3.5)
    assert metrics.get("carto_test_total") == 1
    assert metrics.get("carto_test_total", source_id="src_a") == 2
    assert metrics.get("carto_test_depth") == 3.5
    text = metrics.render()
    assert "# HELP carto_test_total Things." in text
    assert "# TYPE carto_test_total counter" in text
    assert "carto_test_total 1\n" in text
    assert 'carto_test_total{source_id="src_a"} 2\n' in text
    assert "carto_test_depth 3.5\n" in text
    assert text.endswith("\n")


def test_undeclared_metric_is_an_error_and_declared_without_series_renders_zero() -> None:
    metrics = EdgeMetrics()
    with pytest.raises(KeyError):
        metrics.inc("carto_nope_total")
    metrics.counter("carto_zero_total", "Never incremented.")
    assert "carto_zero_total 0\n" in metrics.render()


def test_names_labels_and_kinds_are_validated() -> None:
    metrics = EdgeMetrics()
    with pytest.raises(ValueError, match="metric name"):
        metrics.counter("bad-name", "x")
    metrics.counter("carto_ok_total", "x")
    with pytest.raises(ValueError, match="already declared"):
        metrics.gauge("carto_ok_total", "x")
    with pytest.raises(ValueError, match="label name"):
        metrics.get("carto_ok_total", **{"bad-label": "x"})
    with pytest.raises(ValueError, match="cannot decrease"):
        metrics.inc("carto_ok_total", -1)
    with pytest.raises(ValueError, match="use inc"):
        metrics.set("carto_ok_total", 1)


def test_label_values_are_escaped_and_truncated() -> None:
    metrics = EdgeMetrics()
    metrics.counter("carto_label_total", "x")
    metrics.inc("carto_label_total", route='/a"b\\c\nd')
    assert 'route="/a\\"b\\\\c\\nd"' in metrics.render()
    metrics.inc("carto_label_total", route="x" * 500)
    assert metrics.get("carto_label_total", route="x" * 128) == 1


def test_series_cap_drops_new_label_combinations() -> None:
    metrics = EdgeMetrics()
    metrics.counter("carto_cap_total", "x")
    for index in range(MAX_SERIES_PER_METRIC + 5):
        metrics.inc("carto_cap_total", source_id=f"src_{index}")
    assert metrics.get("carto_cap_total", source_id="src_0") == 1
    assert metrics.get("carto_cap_total", source_id=f"src_{MAX_SERIES_PER_METRIC + 1}") == 0
    # An existing series still counts past the cap.
    metrics.inc("carto_cap_total", source_id="src_0")
    assert metrics.get("carto_cap_total", source_id="src_0") == 2


def test_default_metrics_declares_every_edge_series() -> None:
    metrics = default_metrics()
    text = metrics.render()
    for name, kind, _help in EDGE_SERIES:
        assert f"# TYPE {name} {kind}" in text
    assert "carto_edge_buffer_depth 0" in text
    assert repr(metrics) == f"EdgeMetrics(metrics={len(EDGE_SERIES)})"
