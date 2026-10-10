"""Options for the leak test over a full simulator run (``make leak SCENARIO=...``, spec 18.3)."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("carto-leak", "carto leak test (spec 18.3)")
    group.addoption("--scenario", default="shop", help="Simulator scenario to scan (default shop)")
    group.addoption(
        "--sim-out", default="sim-out", help="Directory of simulator runs and bundles (sim-out)"
    )
