"""Run ``carto-core api`` under uvicorn with TLS (spec 12, 14.4). Package E."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from carto_core.settings import CoreSettings

__all__ = ["run_api"]


def run_api(settings: CoreSettings) -> int:
    del settings
    msg = "package E builds the console API"
    raise NotImplementedError(msg)
