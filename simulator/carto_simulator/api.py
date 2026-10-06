"""Programmatic entry point: ``generate(request, out_dir)``. The CLI and the eval harness call this.

Scenarios register in :mod:`carto_simulator.scenarios`. Generation is deterministic: the same
request produces byte-identical output (spec 19, "Deterministic by seed").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from carto_simulator.ground_truth import Manifest

GENERATOR_VERSION = "0.1.0"

DEFAULT_SCENARIO = "shop"
DEFAULT_DAYS = 14
DEFAULT_SEED = 1
DEFAULT_DAILY_VOLUME = 800
DEFAULT_START_DATE = date(2026, 9, 23)  # a Wednesday; puts the spec's fault days on weekdays
DEFAULT_NOISE_RATE = 1.0
DEFAULT_PII_DENSITY = 0.3


@dataclass(frozen=True)
class GenerationRequest:
    """Inputs per spec 19: scenario, days, daily volume, seed, faults, noise, PII density."""

    scenario: str = DEFAULT_SCENARIO
    days: int = DEFAULT_DAYS
    seed: int = DEFAULT_SEED
    daily_volume: int = DEFAULT_DAILY_VOLUME
    start_date: date = DEFAULT_START_DATE
    faults: bool = True
    noise_rate: float = DEFAULT_NOISE_RATE
    pii_density: float = DEFAULT_PII_DENSITY

    def __post_init__(self) -> None:
        if self.days < 1:
            msg = "days must be at least 1"
            raise ValueError(msg)
        if self.daily_volume < 1:
            msg = "daily_volume must be at least 1"
            raise ValueError(msg)
        if not 0.0 <= self.noise_rate <= 10.0:
            msg = "noise_rate must be between 0 and 10 (multiplier on the scenario's noise)"
            raise ValueError(msg)
        if not 0.0 <= self.pii_density <= 1.0:
            msg = "pii_density must be between 0 and 1"
            raise ValueError(msg)


@dataclass(frozen=True)
class GenerationResult:
    out_dir: Path
    ground_truth_dir: Path
    manifest: Manifest


class Scenario(Protocol):
    """A scenario writes native files under ``out_dir`` and ``ground_truth/`` next to them."""

    name: str
    description: str

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult: ...


def generate(request: GenerationRequest, out_dir: Path) -> GenerationResult:
    """Generate ``request.scenario`` into ``out_dir`` (created if missing, replaced if present)."""
    from carto_simulator.scenarios import get_scenario  # noqa: PLC0415  (registry import cycle)

    scenario = get_scenario(request.scenario)
    return scenario.generate(request, out_dir)
