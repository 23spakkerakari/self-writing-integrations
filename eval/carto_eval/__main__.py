"""``python -m carto_eval`` runs the ``carto-eval`` command line."""

from __future__ import annotations

import sys

from carto_eval.cli import main

if __name__ == "__main__":
    sys.exit(main())
