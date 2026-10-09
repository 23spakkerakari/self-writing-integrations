"""Exit codes shared by the carto-ctl parser (:mod:`carto_ctl.registry`) and its handlers.

They live apart from the registry so a handler module can import them without importing the
registry that imports the handler.
"""

from __future__ import annotations

from typing import Final

__all__ = ["EXIT_FAILURE", "EXIT_NOT_IMPLEMENTED", "EXIT_OK", "EXIT_USAGE"]

EXIT_OK: Final = 0
"""The command did what it says, or ``--help`` was printed."""

EXIT_FAILURE: Final = 1
"""The command ran and failed (a refusal to overwrite, an unreachable KMS, an I/O error)."""

EXIT_USAGE: Final = 2
"""Usage error, missing subcommand, or unknown command (argparse's own convention)."""

EXIT_NOT_IMPLEMENTED: Final = 2
"""A command this build does not implement yet. Never 0: a script must not mistake a stub for
success."""
