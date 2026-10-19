"""``carto-eval fit-weights`` (plan M2 decision 4; ADR 0049 item 1) on a small shop run: the
samples are labelled from truth after mapping, the fitted file is a valid weights document that
``load_weights`` accepts and the scorer runs with, the version names the data, and core never
imports scikit-learn. ``bench-linker`` runs at a toy size."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carto_core.linker.score import Scorer
from carto_core.linker.weights import FEATURE_NAMES, Weights, load_weights
from carto_core.settings import CoreSettings, LinkerSettings
from carto_edge.pipeline.pii import RegexDetector
from carto_eval import bench_linker, cli, fit_weights
from carto_eval.fit_weights import LabeledSample, ScenarioSamples

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core" / "carto_core"
TODAY = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture(scope="module")
def fitted(tmp_path_factory: pytest.TempPathFactory) -> fit_weights.FitResult:
    root = tmp_path_factory.mktemp("fit")
    return fit_weights.run_fit(
        ["shop"],
        sim_root=root,
        out=root / "weights" / "fitted.json",
        days=3,
        daily_volume=200,
        settings=CoreSettings(_env_file=None),
        detector=RegexDetector(),
        today=TODAY,
    )


def test_fit_writes_a_valid_weights_file(fitted: fit_weights.FitResult) -> None:
    weights = load_weights(fitted.path)
    assert weights == fitted.weights
    assert weights.version.startswith("fitted-20261018-") and len(weights.version) == 24
    assert weights.fitted_on is not None and weights.fitted_on.startswith("shop (3 days, ")
    assert set(weights.coefficients) == set(FEATURE_NAMES)
    document = json.loads(fitted.path.read_text(encoding="utf-8"))
    assert Weights.model_validate(document) == weights
    # the scorer accepts the fitted set as it accepts the shipped one
    scorer = Scorer(LinkerSettings(), weights)
    assert scorer.weights.version == weights.version
    assert "wrote" in fitted.lines()[-1]


def test_samples_are_labelled_from_truth(fitted: fit_weights.FitResult) -> None:
    (scenario,) = fitted.scenarios
    assert scenario.scenario == "shop" and scenario.days == 3
    assert scenario.candidates > scenario.positives > 0
    assert scenario.same_field > 0  # order_id across the order log's templates
    assert all(set(sample.vector) == set(FEATURE_NAMES) for sample in scenario.samples)
    assert all("t1." not in sample.a and "t1." not in sample.b for sample in scenario.samples)
    assert {sample.source for sample in scenario.samples} <= {"lsh", "bridge"}
    overall = fitted.overall
    assert overall.positives == scenario.positives
    assert overall.precision is None or 0.0 <= overall.precision <= 1.0
    assert overall.recall is None or 0.0 <= overall.recall <= 1.0


def test_fit_needs_both_classes() -> None:
    one = ScenarioSamples(
        "x",
        1,
        tuple(
            LabeledSample("x", dict.fromkeys(FEATURE_NAMES, 0.5), True, "lsh", "a", "b")
            for _ in range(3)
        ),
        3,
        0,
    )
    with pytest.raises(fit_weights.FitError, match="both classes"):
        fit_weights.fit([one], today=TODAY)


def test_version_is_a_function_of_the_samples() -> None:
    sample = LabeledSample("x", dict.fromkeys(FEATURE_NAMES, 0.5), True, "lsh", "a", "b")
    other = LabeledSample("x", dict.fromkeys(FEATURE_NAMES, 0.4), True, "lsh", "a", "b")
    first = fit_weights.weights_version([sample], TODAY)
    assert first == fit_weights.weights_version([sample], TODAY)
    assert first != fit_weights.weights_version([other], TODAY)
    assert first.startswith("fitted-20261018-")


def test_core_never_imports_sklearn() -> None:
    """ADR 0049 item 1: scikit-learn is an eval-only dependency."""
    pattern = re.compile(r"^\s*(from|import)\s+sklearn\b", re.MULTILINE)
    offenders = [
        path for path in CORE.rglob("*.py") if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    assert pattern.search((ROOT / "eval" / "carto_eval" / "fit_weights.py").read_text("utf-8"))
    pyproject = (ROOT / "core" / "pyproject.toml").read_text(encoding="utf-8")
    assert "scikit-learn" not in pyproject and "sklearn" not in pyproject


def test_cli_fit_weights_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = str(tmp_path / "w.json")
    code = cli.main(
        [
            "fit-weights",
            "--scenario",
            "shop",
            "--days",
            "0",
            "--sim-out",
            str(tmp_path),
            "--out",
            out,
        ]
    )
    assert code == 2
    assert "days must be at least 1" in capsys.readouterr().err


def test_bench_linker_runs_at_toy_size(capsys: pytest.CaptureFixture[str]) -> None:
    result = bench_linker.run_bench(
        systems=3, fields_per_system=2, tokens_per_field=60, verify_cap=4
    )
    assert result.fields == 6 and result.planted_pairs == 4
    assert result.verified <= 4 and result.candidate_seconds >= 0.0
    assert any("bench-linker: 3 systems x 2 fields" in line for line in result.lines())
    args = [
        "bench-linker",
        "--systems",
        "2",
        "--fields",
        "2",
        "--tokens",
        "60",
        "--verify-cap",
        "2",
    ]
    assert cli.main(args) == 0
    assert "measured, not gating" in capsys.readouterr().out
    assert cli.main(["bench-linker", "--systems", "0"]) == 2
