"""``carto-ctl`` entry point: an argparse tree built from the command registry (spec Section 20).

The parser is generated from :data:`carto_ctl.registry.COMMANDS`, so adding or implementing a
command is a registry edit, not a parser edit. Exit codes: 0 for success and ``--help``; 2 for
a usage error, a missing subcommand, or a command this build does not implement yet. Option
abbreviation is off at every level of the tree: ``--he`` is a usage error, not ``--help``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from carto_ctl import __version__
from carto_ctl.registry import (
    COMMANDS,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_USAGE,
    GROUPS,
    PROG,
    RESERVED_DESTS,
    Command,
    Invocation,
)

type _Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def build_parser(commands: Sequence[Command] = COMMANDS) -> argparse.ArgumentParser:
    """Build the full command tree: one subparser per group, one leaf per command.

    Each parser records itself as ``help_parser`` so :func:`run` can print the right help when
    a group is given without a subcommand. Leaves record their :class:`Command` as ``command``.
    A ``configure`` hook that reuses either key (:data:`RESERVED_DESTS`) is a ``ValueError``.
    """
    _validate(commands)
    root = argparse.ArgumentParser(
        prog=PROG,
        description="carto operator CLI: PKI, keys, audit verification, support bundles, drills.",
        epilog=_command_table(commands),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    root.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")
    root.set_defaults(command=None, help_parser=root)
    subparsers: dict[tuple[str, ...], _Subparsers] = {
        (): root.add_subparsers(title="commands", metavar="<command>")
    }
    for command in commands:
        for depth in range(1, len(command.path)):
            prefix = command.path[:depth]
            if prefix not in subparsers:
                subparsers[prefix] = _add_group(subparsers[prefix[:-1]], prefix[-1])
        leaf = subparsers[command.path[:-1]].add_parser(
            command.path[-1],
            help=command.summary,
            description=_description(command),
            allow_abbrev=False,
        )
        leaf.set_defaults(command=command, help_parser=leaf)
        if command.configure is not None:
            command.configure(leaf)
            _check_reserved_dests(command, leaf)
    return root


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``carto-ctl = "carto_ctl.cli:main"``); never raises SystemExit."""
    return run(sys.argv[1:] if argv is None else argv, COMMANDS)


def run(argv: Sequence[str], commands: Sequence[Command]) -> int:
    """Parse ``argv`` against ``commands`` and dispatch to the matching handler."""
    parser = build_parser(commands)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits itself: 0 after printing --help or --version, 2 on a usage error.
        return _exit_code(exc)
    command: Command | None = args.command
    if command is None:
        # Bare ``carto-ctl`` or a group without a subcommand: show the help, refuse to succeed.
        args.help_parser.print_help()
        return EXIT_USAGE
    return command.handler(Invocation(command, args, sys.stdout, sys.stderr))


def _exit_code(exc: SystemExit) -> int:
    """Translate argparse's SystemExit into a return code (None means 0, as for sys.exit)."""
    code = exc.code
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    return EXIT_USAGE


def _add_group(parent: _Subparsers, group: str) -> _Subparsers:
    """Create the subparser for a command group such as ``pki`` and return its subcommand slot."""
    group_help = GROUPS[group]
    parser = parent.add_parser(
        group, help=group_help, description=f"{group_help}.", allow_abbrev=False
    )
    parser.set_defaults(command=None, help_parser=parser)
    return parser.add_subparsers(title="subcommands", metavar="<subcommand>")


def _check_reserved_dests(command: Command, leaf: argparse.ArgumentParser) -> None:
    """Reject a ``configure`` hook that touched a namespace key :func:`run` dispatches on."""
    clashes = sorted(RESERVED_DESTS.intersection(action.dest for action in leaf._actions))
    if clashes:
        msg = f"{command.name!r}: configure hook added an option with reserved dest {clashes}"
        raise ValueError(msg)
    if leaf.get_default("command") is not command or leaf.get_default("help_parser") is not leaf:
        msg = f"{command.name!r}: configure hook replaced a reserved default (RESERVED_DESTS)"
        raise ValueError(msg)


def _description(command: Command) -> str:
    """Long help for a leaf: what it does, the spec section and, for a stub, when it arrives."""
    text = f"{command.help} (spec {command.section})."
    if command.implemented:
        return text
    return (
        f"{text} Arrives in {command.milestone}; this build prints a notice and exits with "
        f"status {EXIT_NOT_IMPLEMENTED}."
    )


def _command_table(commands: Sequence[Command]) -> str:
    """Epilog for the root ``--help``: every command with its one-line help."""
    width = max((len(command.name) for command in commands), default=0)
    rows = [f"  {command.name:<{width}}  {command.summary}" for command in commands]
    return "all commands:\n" + "\n".join(rows)


def _validate(commands: Sequence[Command]) -> None:
    """Reject a registry the parser could not represent unambiguously.

    Problems are reported in registry order, so one registry always fails with one message.
    """
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        if not command.path or not all(command.path):
            msg = f"command path must be one or more non-empty words, got {command.path!r}"
            raise ValueError(msg)
        if command.path in seen:
            msg = f"duplicate command: {command.name!r}"
            raise ValueError(msg)
        seen.add(command.path)
    for command in commands:
        for depth in range(1, len(command.path)):
            prefix = command.path[:depth]
            if prefix in seen:
                msg = f"{' '.join(prefix)!r} is both a command and a group"
                raise ValueError(msg)
            if prefix[-1] not in GROUPS:
                msg = f"command group {prefix[-1]!r} has no help text in GROUPS"
                raise ValueError(msg)


if __name__ == "__main__":
    raise SystemExit(main())
