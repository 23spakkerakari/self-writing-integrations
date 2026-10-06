"""Command registry for carto-ctl (spec Section 20, ``tools/carto-ctl/``).

Every operator command is one frozen :class:`Command`: its path (``pki init``), the milestone
that implements it, the spec section it implements and the handler that runs it. ``cli.py``
builds the argparse tree from :data:`COMMANDS`, so a later milestone replaces a stub by swapping
the ``handler`` (and, if it needs options, the ``configure`` hook) of one entry and touches
nothing else.

Handlers never print: they write to the streams of their :class:`Invocation`, which keeps them
testable and keeps console output in one place.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TextIO

from carto_ctl import __version__

PROG = "carto-ctl"

EXIT_OK = 0
"""The command did what it says, or ``--help`` was printed."""

EXIT_USAGE = 2
"""Usage error, missing subcommand, or unknown command (argparse's own convention)."""

EXIT_NOT_IMPLEMENTED = 2
"""A command this build does not implement yet. Never 0: a script must not mistake a stub for
success."""

type Handler = Callable[[Invocation], int]
"""Runs one command and returns the process exit code."""

type Configure = Callable[[argparse.ArgumentParser], None]
"""Adds a command's own options to its (already created) argparse subparser.

The hook may not add an option whose ``dest`` is in :data:`RESERVED_DESTS` or override either
default; :func:`carto_ctl.cli.build_parser` rejects a hook that does.
"""

RESERVED_DESTS: frozenset[str] = frozenset({"command", "help_parser"})
"""Namespace keys the parser sets on every command for :func:`carto_ctl.cli.run`: the
:class:`Command` to dispatch to and the parser whose help to print. No option may use them as its
``dest``."""


@dataclass(frozen=True, slots=True)
class Invocation:
    """What a handler receives: the registry entry, parsed arguments and the output streams."""

    command: Command
    args: argparse.Namespace
    out: TextIO
    err: TextIO


@dataclass(frozen=True, slots=True)
class Command:
    """One operator command (spec Section 20 layout line for ``tools/carto-ctl/``).

    ``path`` is the command words in order (``("pki", "init")``); every prefix is a command
    group. ``section`` is the spec section the command implements and ``milestone`` the
    milestone that delivers it.
    """

    path: tuple[str, ...]
    help: str
    milestone: str
    section: str
    handler: Handler
    configure: Configure | None = None

    @property
    def name(self) -> str:
        """The command as typed: ``pki init``."""
        return " ".join(self.path)

    @property
    def summary(self) -> str:
        """One-line help citing the spec section and the milestone, for ``--help`` listings."""
        return f"{self.help} (spec {self.section}, {self.milestone})"

    @property
    def implemented(self) -> bool:
        """False while the entry still points at :func:`stub`."""
        return self.handler is not stub


def stub(invocation: Invocation) -> int:
    """Placeholder handler: one line naming the milestone and spec section, exit code 2."""
    command = invocation.command
    invocation.out.write(
        f"{PROG} {command.name}: not implemented in this build; "
        f"arrives in {command.milestone} (spec {command.section}).\n"
    )
    return EXIT_NOT_IMPLEMENTED


def version(invocation: Invocation) -> int:
    """``carto-ctl version``: print ``carto-ctl <version>`` and succeed."""
    invocation.out.write(f"{PROG} {__version__}\n")
    return EXIT_OK


GROUPS: Mapping[str, str] = MappingProxyType(
    {
        "pki": "Private CA and certificates for internal mutual TLS (spec 14.4)",
        "key": "Tokenization key management (spec 8.4)",
        "audit": "Audit log hash chain (spec 14.9)",
        "drill": "Scripted operational drills (spec 14.11)",
    }
)
"""Help text for every command group, keyed by the group word."""

COMMANDS: tuple[Command, ...] = (
    Command(
        path=("pki", "init"),
        help="Create the private CA for internal mutual TLS",
        milestone="M1",
        section="14.4",
        handler=stub,
    ),
    Command(
        path=("key", "init"),
        help="Generate the tenant tokenization key, wrapped by the customer KMS",
        milestone="M1",
        section="8.4",
        handler=stub,
    ),
    Command(
        path=("audit", "verify"),
        help="Verify the audit log hash chain against its daily anchors",
        milestone="M6",
        section="14.9",
        handler=stub,
    ),
    Command(
        path=("support-bundle",),
        help="Write a redacted support bundle: versions, config, metrics, product logs",
        milestone="M6",
        section="16",
        handler=stub,
    ),
    Command(
        path=("drill", "restore"),
        help="Run the scripted backup restore drill against a test stack",
        milestone="M6",
        section="14.11",
        handler=stub,
    ),
    Command(
        path=("verify-signatures",),
        help="Verify cosign signatures and provenance of release images and SBOMs",
        milestone="M6",
        section="14.8",
        handler=stub,
    ),
    Command(
        path=("version",),
        help="Print the carto-ctl version",
        milestone="M0",
        section="20",
        handler=version,
    ),
)
"""The command table, in the order ``--help`` lists it."""
