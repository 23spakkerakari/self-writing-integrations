"""``carto-sim``: batch-mode simulator command line (spec 19, M0 ``make sim``).

    carto-sim generate --scenario shop --days 14 --seed 1 --daily-volume 800 \\
        --start-date 2026-09-23 --out sim-out/shop [--no-faults] [--noise-rate 1.0] \\
        [--pii-density 0.3]
    carto-sim list

Exit codes: 0 on success, 2 for an unknown scenario, an invalid request or an output path
that cannot be used (a file, or a populated directory that is not a previous run).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from carto_simulator import api
from carto_simulator.scenarios import list_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="carto-sim", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="write native files and ground truth")
    generate.add_argument("--scenario", default=api.DEFAULT_SCENARIO)
    generate.add_argument("--days", type=int, default=api.DEFAULT_DAYS)
    generate.add_argument("--seed", type=int, default=api.DEFAULT_SEED)
    generate.add_argument("--daily-volume", type=int, default=api.DEFAULT_DAILY_VOLUME)
    generate.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=api.DEFAULT_START_DATE,
        help="first local day, ISO format (default %(default)s)",
    )
    generate.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output directory; an empty one or a previous run is emptied first",
    )
    generate.add_argument("--no-faults", action="store_true", help="generate a fault-free run")
    generate.add_argument("--noise-rate", type=float, default=api.DEFAULT_NOISE_RATE)
    generate.add_argument("--pii-density", type=float, default=api.DEFAULT_PII_DENSITY)
    commands.add_parser("list", help="print the known scenarios")
    return parser


def _generate(args: argparse.Namespace) -> int:
    try:
        request = api.GenerationRequest(
            scenario=args.scenario,
            days=args.days,
            seed=args.seed,
            daily_volume=args.daily_volume,
            start_date=args.start_date,
            faults=not args.no_faults,
            noise_rate=args.noise_rate,
            pii_density=args.pii_density,
        )
    except ValueError as exc:
        print(f"carto-sim: {exc}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        result = api.generate(request, args.out)
    except (KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) else exc
        print(f"carto-sim: {message}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - started
    counts = result.manifest.counts
    sources = len(counts) - 4  # minus the totals: transactions, events, noise, files
    print(f"scenario {request.scenario}: {request.days} days from {request.start_date}")
    print(f"out: {result.out_dir}")
    print(f"sources: {sources}, events: {counts['events']}, transactions: {counts['transactions']}")
    faults = "on" if request.faults else "off"
    print(f"noise: {counts['noise']}, files: {counts['files']}, faults: {faults}")
    print(f"elapsed: {elapsed:.1f} s")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        for name in list_scenarios():
            print(name)
        return 0
    return _generate(args)


if __name__ == "__main__":
    sys.exit(main())
