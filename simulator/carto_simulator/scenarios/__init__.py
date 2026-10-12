"""Scenario registry. Scenario A (shop) in M0; C (people ops) in M2; B (payer) in M4 (spec 21)."""

from __future__ import annotations

from carto_simulator.api import Scenario


def get_scenario(name: str) -> Scenario:
    """Return the scenario by name; raise ``KeyError`` with the known names otherwise."""
    from carto_simulator.scenarios.shop import ShopScenario  # noqa: PLC0415

    registry: dict[str, Scenario] = {ShopScenario.name: ShopScenario()}
    try:
        return registry[name]
    except KeyError:
        known = ", ".join(sorted(registry))
        msg = f"unknown scenario {name!r}; known scenarios: {known}"
        raise KeyError(msg) from None


def list_scenarios() -> list[str]:
    return ["shop"]
