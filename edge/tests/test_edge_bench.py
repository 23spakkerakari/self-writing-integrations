"""carto-edge bench (spec 17, plan M1): loads records once, cycles the pipeline for the given
time, reports throughput, latency percentiles, drops and the 2,000 events/s verdict, and leaves
no state behind (its temporary key and vault are closed before the directory goes)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from carto_edge.cli.analyze import AnalyzeError
from carto_edge.cli.bench import TARGET_EVENTS_PER_SECOND, BenchResult, bench, format_bench
from carto_edge.cli.main import main
from carto_edge.pipeline.pii import RegexDetector
from carto_simulator.api import GenerationRequest, generate

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "simulator" / "analyze.shop.yaml"


@pytest.fixture(scope="module")
def sim_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("bench-sim") / "shop"
    generate(GenerationRequest(days=1, daily_volume=5, seed=11), out)
    return out


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configured, config = structlog.is_configured(), structlog.get_config()
    yield
    if configured:
        structlog.configure(**config)
    else:
        structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_bench_measures_throughput_and_latency(sim_dir: Path) -> None:
    result = bench(CONFIG, sim_dir, seconds=0.5, max_records=300, detector=RegexDetector())
    assert isinstance(result, BenchResult)
    assert result.records_loaded == 300
    assert result.sources >= 1
    assert result.processed >= 1
    assert result.events + sum(result.dropped.values()) == result.processed
    assert result.seconds >= 0.5
    assert 0 < result.p50_us <= result.p95_us <= result.p99_us
    assert result.events_per_second == pytest.approx(result.events / result.seconds)
    assert result.target_met is (result.events_per_second >= TARGET_EVENTS_PER_SECOND)
    lines = format_bench(result)
    assert any("events/s" in line for line in lines)
    assert any("p99" in line for line in lines)
    assert "spec 17 target" in lines[-1]


def test_the_command_runs_for_one_second(
    sim_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,
) -> None:
    monkeypatch.setenv("CARTO_PII__ENABLED", "false")
    args = ["--input", str(sim_dir), "--config", str(CONFIG), "--max-records", "200"]
    assert main(["bench", *args, "--seconds", "1"]) == 0
    out = capsys.readouterr().out
    assert "carto-edge bench: 200 records" in out
    assert "events/s" in out
    assert "p50" in out
    assert "p95" in out
    assert "p99" in out
    assert f"{TARGET_EVENTS_PER_SECOND:,} events/s sustained:" in out


def test_usage_errors(
    sim_dir: Path, tmp_path: Path, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["bench", "--input", str(tmp_path / "nope"), "--config", str(CONFIG)]) == 2
    assert "not a directory" in capsys.readouterr().err
    with pytest.raises(AnalyzeError) as bad_seconds:
        bench(CONFIG, sim_dir, seconds=0)
    assert bad_seconds.value.code == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AnalyzeError, match="no record") as nothing:
        bench(CONFIG, empty, seconds=0.1, detector=RegexDetector())
    assert nothing.value.code == 1
