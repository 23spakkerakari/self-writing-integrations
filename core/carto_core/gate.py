"""``carto-core gate-report`` (spec 21 M2 gate; founder decision 6). Package I."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["GateError", "gate_report"]


class GateError(Exception):
    """The report could not be built; the message names the file and the place."""


def gate_report(predictions: Path, confirmed: Path) -> str:
    del predictions, confirmed
    msg = "package I builds the gate report"
    raise NotImplementedError(msg)
