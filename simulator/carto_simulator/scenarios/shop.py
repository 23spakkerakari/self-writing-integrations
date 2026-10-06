"""Scenario A, "shop" (spec 19): webstore, order system, payments, warehouse, shipping.

Implemented in M0. This stub fixes the contract; see docs/plans/M0.md for the scenario design.
"""

from __future__ import annotations

from pathlib import Path

from carto_simulator.api import GenerationRequest, GenerationResult


class ShopScenario:
    name = "shop"
    description = "Scenario A: an order's journey across five systems, with manual PO entry."

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult:
        msg = f"scenario {self.name!r} is not implemented yet ({request.days} days to {out_dir})"
        raise NotImplementedError(msg)
