"""``carto-core worker``: lease profile, link and family jobs from PostgreSQL (spec 5.3).
Package A builds the loop; packages B and C hand it the job bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from carto_core.settings import CoreSettings

__all__ = ["run_worker"]


def run_worker(settings: CoreSettings, *, once: bool = False) -> int:
    del settings, once
    msg = "package A builds the worker"
    raise NotImplementedError(msg)
