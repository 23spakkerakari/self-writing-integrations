"""``carto-edge``: the offline analyzer, the edge gateway, the benchmark and audit verification.

    carto-edge analyze --config FILE --input DIR --out DIR [--state-dir DIR] [--tenant-id ID]
                       [--locator-map] [--producer TEXT]
    carto-edge gateway
    carto-edge bench --input DIR --config FILE [--seconds N] [--max-records N] [--path P]
    carto-edge audit verify --file PATH
    carto-edge key {status,rotate,rewrap}
    carto-edge secret {set,list,delete}
    carto-edge source test [--config FILE] [--source ID ...]

``key``, ``secret`` and ``source`` are the operator commands of :mod:`carto_edge.cli.admin`.
``gateway`` reads its settings from the environment (``CARTO_...``,
:class:`carto_edge.config.EdgeSettings`). Exit codes: 0 success, 1 the operation failed (for
``audit verify``: the chain is broken), 2 a usage or configuration error. Handlers return codes;
only ``__main__`` exits. Product logs are JSON lines on standard error (the analyzer and the
benchmark print their summary on standard output) and pass the redaction processor of
:mod:`carto_common.logging` (spec 2.3 invariant 7).
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, TextIO, cast

from pydantic import ValidationError

from carto_common.logging import configure_logging
from carto_edge.audit import verify as verify_audit
from carto_edge.cli import admin
from carto_edge.cli.analyze import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    add_analyze_arguments,
    describe,
    emit,
    run_analyze,
)
from carto_edge.cli.bench import add_bench_arguments, run_bench
from carto_edge.config import EdgeSettings

__all__ = ["EXIT_FAILURE", "EXIT_OK", "EXIT_USAGE", "PROG", "build_parser", "main"]

PROG: Final = "carto-edge"
GATEWAY_MODULE: Final = "carto_edge.gateway.server"
LOG_LEVELS: Final = ("critical", "error", "warning", "info", "debug")


class _LateStderr:
    """Writes to whatever ``sys.stderr`` is when a line is logged, so a swapped stream (a test
    harness, a redirect) never receives a write after it was closed."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "carto edge: offline analyzer, gateway, benchmark, audit verification, keys, "
            "local secrets and connector tests."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--log-level", choices=LOG_LEVELS, default="info", help="Product log level (stderr)"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    analyze = commands.add_parser(
        "analyze", help="Turn exported files into a signed bundle (spec 4.1)", allow_abbrev=False
    )
    add_analyze_arguments(analyze)
    commands.add_parser("gateway", help="Run the edge gateway with settings from the environment")
    bench = commands.add_parser(
        "bench", help="Measure pipeline throughput (spec 17)", allow_abbrev=False
    )
    add_bench_arguments(bench)
    audit = commands.add_parser("audit", help="Edge audit trail tools")
    actions = audit.add_subparsers(dest="audit_command", required=True, metavar="<action>")
    verify = actions.add_parser("verify", help="Recompute the hash chain of an audit file")
    verify.add_argument("--file", type=Path, required=True, help="Path of audit.ndjson")
    admin.add_commands(commands)
    return parser


def _configure_cli_logging(level: str) -> None:
    configure_logging(PROG, level, json_output=True, stream=cast("TextIO", _LateStderr()))


def _gateway() -> int:
    try:  # imported here: the gateway stack (uvicorn, FastAPI, TLS) is not needed elsewhere
        module = importlib.import_module(GATEWAY_MODULE)
        run_gateway = cast("Callable[[EdgeSettings], int]", module.run_gateway)
    except (ImportError, AttributeError) as exc:
        print(f"{PROG}: the gateway is not available: {describe(exc)}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        settings = EdgeSettings()
    except ValidationError as exc:
        print(f"{PROG}: invalid settings: {describe(exc)}", file=sys.stderr)
        return EXIT_USAGE
    return run_gateway(settings)


def _audit_verify(path: Path) -> int:
    if not path.is_file():
        print(f"{PROG}: {path} is not a file", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = verify_audit(path)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{PROG}: cannot read {path}: {describe(exc)}", file=sys.stderr)
        return EXIT_FAILURE
    if result.ok:
        emit(f"audit chain {path}: {result.rows} rows, intact")
        return EXIT_OK
    emit(
        f"audit chain {path}: {result.rows} rows, BROKEN at line {result.first_bad_line} "
        "(that row or one before it was changed, removed or reordered)"
    )
    return EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit code instead of exiting."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE
    if args.command == "gateway":
        configure_logging(PROG, args.log_level, json_output=True)
        return _gateway()
    _configure_cli_logging(args.log_level)
    if args.command == "analyze":
        return run_analyze(args)
    if args.command == "bench":
        return run_bench(args)
    if args.command in {"key", "secret", "source"}:
        try:
            return admin.run(args)
        except ValidationError as exc:
            print(f"{PROG}: invalid settings: {describe(exc)}", file=sys.stderr)
            return EXIT_USAGE
    return _audit_verify(args.file)


if __name__ == "__main__":
    sys.exit(main())
