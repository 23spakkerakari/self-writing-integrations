"""``carto-eval``: score engine output against simulator ground truth (spec 18.4, ``make eval``).

    carto-eval run --scenario shop --days 14 --seed 1 --sim-out sim-out/shop --out eval/reports \\
        [--predictions DIR] [--regenerate] [--tolerance 2] [--enforce-targets] \\
        [--enforce-regressions] [--daily-volume 800]
    carto-eval self-check --sim-out DIR [--scenario shop] [--days 14] [--seed 1] \\
        [--daily-volume 800]

``run`` generates the scenario when ``DIR/ground_truth`` is missing or ``--regenerate`` is
given, and never otherwise: an existing ground truth generated with another scenario, day count,
seed or daily volume is refused (exit 2) rather than replaced, and so is an incomplete or
unreadable one. It scores ``--predictions`` (or the empty prediction when the option is absent or
the directory does not exist); writes ``<out>/<scenario>.json`` and ``<out>/<scenario>.md``;
appends ``<out>/history.ndjson``; prints the Markdown report. Exit 0, or 1 when
``--enforce-targets`` and a metric fails or ``--enforce-regressions`` and a metric regressed
beyond the tolerance; 2 for a usage error, a ground truth that cannot be scored, a prediction
file that cannot be read, an output directory the simulator refuses or a report that cannot be
written.

``self-check`` scores the ground truth against itself and returns 0 only when the result is
perfect (see :func:`carto_eval.scoring.self_check_problems`).

``generated_at`` is taken from the wall clock here and nowhere else.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from carto_eval.predictions import Predictions, PredictionsError
from carto_eval.report import (
    HISTORY_FILE,
    EvalReport,
    append_history,
    build_report,
    find_regressions,
    last_record,
    read_history,
)
from carto_eval.scoring import (
    SIMULATOR_OPTIONS,
    Scorecard,
    TruthEvents,
    check_complete,
    describe_error,
    ensure_ground_truth,
    load_truth_events,
    parameter_differences,
    report_counts,
    score,
    self_check_problems,
)
from carto_eval.targets import DEFAULT_TOLERANCE_POINTS
from carto_simulator import api
from carto_simulator.ground_truth import GroundTruth, iter_events, read_ground_truth


def build_parser() -> argparse.ArgumentParser:
    """The ``carto-eval`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="carto-eval",
        description="Score engine output against simulator ground truth (spec 18.4).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="score predictions (or the empty prediction)")
    _add_scenario_arguments(run)
    run.add_argument("--sim-out", type=Path, required=True, help="simulator output directory")
    run.add_argument("--out", type=Path, required=True, help="report directory")
    run.add_argument("--predictions", type=Path, help="engine output directory to score")
    run.add_argument(
        "--regenerate", action="store_true", help="regenerate the scenario even if present"
    )
    run.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE_POINTS,
        help="regression tolerance in points for ratio metrics (default %(default)s)",
    )
    run.add_argument(
        "--enforce-targets", action="store_true", help="exit 1 when a metric fails its target"
    )
    run.add_argument(
        "--enforce-regressions", action="store_true", help="exit 1 when a metric regressed"
    )

    check = commands.add_parser("self-check", help="score the ground truth against itself")
    _add_scenario_arguments(check)
    check.add_argument("--sim-out", type=Path, required=True, help="simulator output directory")
    return parser


def _add_scenario_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scenario", default=api.DEFAULT_SCENARIO)
    parser.add_argument("--days", type=int, default=api.DEFAULT_DAYS)
    parser.add_argument("--seed", type=int, default=api.DEFAULT_SEED)
    parser.add_argument("--daily-volume", type=int, default=api.DEFAULT_DAILY_VOLUME)


def _emit(text: str) -> None:
    """Print without failing on characters the console encoding cannot show (ADR 0007)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding))


def _fail(message: str) -> int:
    print(f"carto-eval: {message}", file=sys.stderr)
    return 2


def _prepare(args: argparse.Namespace) -> tuple[GroundTruth, TruthEvents] | int:
    """Generate when needed, then read the ground truth; an int is an exit code."""
    try:
        request = api.GenerationRequest(
            scenario=args.scenario, days=args.days, seed=args.seed, daily_volume=args.daily_volume
        )
    except ValueError as exc:
        return _fail(str(exc))
    try:
        reason = ensure_ground_truth(
            request, args.sim_out, regenerate=bool(getattr(args, "regenerate", False))
        )
    except (KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) else exc
        return _fail(str(message))
    if reason is not None:
        _emit(f"generated scenario {request.scenario} into {args.sim_out} ({reason})")
    try:
        truth = read_ground_truth(args.sim_out)
        events = load_truth_events(args.sim_out)
        check_complete(truth, events)
    except (OSError, ValueError, TypeError) as exc:
        return _fail(
            f"ground truth under {args.sim_out} is incomplete or unreadable "
            f"({describe_error(exc)}); rerun with --regenerate"
        )
    options = parameter_differences(truth.manifest, request, SIMULATOR_OPTIONS)
    if options:
        print(
            f"carto-eval: ground truth under {args.sim_out} was generated with {options}; "
            "scoring it as it is",
            file=sys.stderr,
        )
    return truth, events


def _report(
    args: argparse.Namespace,
    truth: GroundTruth,
    events: TruthEvents,
    predictions: Predictions,
) -> tuple[EvalReport, Scorecard]:
    scorecard = score(truth, events.membership, predictions)
    report = build_report(
        scenario=args.scenario,
        seed=args.seed,
        days=args.days,
        generated_at=datetime.now(UTC),
        predictions_present=predictions.present,
        counts=report_counts(truth, events, predictions),
        values=scorecard.values(),
    )
    return report, scorecard


def _run(args: argparse.Namespace) -> int:
    prepared = _prepare(args)
    if isinstance(prepared, int):
        return prepared
    truth, events = prepared
    predictions = Predictions.empty()
    if args.predictions is not None:
        try:
            predictions = Predictions.load(args.predictions)
        except PredictionsError as exc:
            return _fail(f"cannot read predictions: {exc}")
        if not predictions.present:
            print(
                f"carto-eval: no predictions at {args.predictions}; scoring the empty prediction",
                file=sys.stderr,
            )
    report, _ = _report(args, truth, events, predictions)

    out: Path = args.out
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail(f"cannot create {out}: {exc}")
    history_path = out / HISTORY_FILE
    try:
        records = read_history(history_path)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read history: {exc}")
    previous = last_record(records, report.scenario, report.seed, report.days)
    report = report.with_regressions(
        find_regressions(previous, report, tolerance_points=args.tolerance)
    )
    json_path = out / f"{report.scenario}.json"
    markdown_path = out / f"{report.scenario}.md"
    try:
        report.write_json(json_path)
        report.write_markdown(markdown_path)
        append_history(history_path, report.history_record())
    except OSError as exc:
        return _fail(f"cannot write to {out}: {exc}")
    _emit(report.to_markdown())
    _emit(f"wrote {json_path} and {markdown_path}")

    code = 0
    failed = report.failed_metrics()
    if args.enforce_targets and failed:
        print(f"carto-eval: metrics below target: {', '.join(failed)}", file=sys.stderr)
        code = 1
    if args.enforce_regressions and report.regressions:
        print(f"carto-eval: {len(report.regressions)} regression(s)", file=sys.stderr)
        code = 1
    return code


def _self_check(args: argparse.Namespace) -> int:
    prepared = _prepare(args)
    if isinstance(prepared, int):
        return prepared
    truth, events = prepared
    predictions = Predictions.from_truth(truth, iter_events(args.sim_out))
    report, scorecard = _report(args, truth, events, predictions)
    _emit(report.to_markdown())
    problems = self_check_problems(report, scorecard)
    if problems:
        for problem in problems:
            print(f"carto-eval: self-check: {problem}", file=sys.stderr)
        return 1
    _emit("self-check: perfect (the ground truth scores 1.0 against itself)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit code instead of exiting."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse reports a usage error (2) or --help (0) by exiting; hand back the code.
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    if args.command == "self-check":
        return _self_check(args)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
