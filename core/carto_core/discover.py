"""``carto-core discover``: one profile and link pass, prediction files out (plan M2 step 6).
Package I builds it on the profiler and linker."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from carto_core.settings import CoreSettings

__all__ = ["DiscoverError", "run_discover"]


class DiscoverError(Exception):
    """Discovery could not run; the message never carries data."""


def run_discover(
    settings: CoreSettings, *, out_dir: Path, bundle: Path | None, as_of: str | None
) -> list[str]:
    del settings, out_dir, bundle, as_of
    msg = "package I builds discover"
    raise NotImplementedError(msg)
