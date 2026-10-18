"""The committed OpenAPI document of the console API (plan M2, "Interfaces fixed").

``carto-core openapi`` renders it from the FastAPI application; ``core/openapi/openapi.v1.json``
is the committed copy the web client is generated from and the authorization matrix test reads.
A drift test fails when the file and the application disagree. Package E implements
:func:`render_openapi` on top of :func:`carto_core.api.app.create_app`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = ["OPENAPI_FILE", "render_openapi"]

OPENAPI_FILE: Final = Path(__file__).resolve().parents[2] / "openapi" / "openapi.v1.json"


def render_openapi() -> str:
    """The OpenAPI 3.1 document as sorted, two-space-indented JSON with a trailing newline."""
    msg = "package E builds the console API"
    raise NotImplementedError(msg)
