"""Entry point for ``carto-edge``. The offline analyzer and gateway arrive in M1."""

from __future__ import annotations

import sys

NOT_YET = "carto-edge: the edge pipeline and offline analyzer arrive in M1 (docs/plans/M1.md)."


def main(argv: list[str] | None = None) -> int:
    """Print the milestone notice and return 2 so scripts cannot mistake this for success."""
    _ = argv if argv is not None else sys.argv[1:]
    print(NOT_YET)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
