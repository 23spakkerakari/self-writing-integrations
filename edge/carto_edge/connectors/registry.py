"""Connector registry: type name to factory, and the shared helpers connectors use (spec 8.1).

``build_connector`` is what the gateway scheduler and the offline analyzer call with a
:class:`SourceConfig`; the connector validates its own ``config`` block (spec 8.1
``validate_config``) and any pydantic error becomes an :class:`InvalidConfigError` whose
message names the location of the problem but never echoes the input (spec 14.3).

The built-in connectors register themselves with :func:`register` when their module is
imported; :func:`build_connector` imports them lazily so importing this module stays cheap and
free of driver imports.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping
from typing import Any, Final, TypeVar

from pydantic import ValidationError

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorFactory,
    InvalidConfigError,
    ReadConnector,
)

__all__ = [
    "CONNECTOR_TYPES",
    "build_connector",
    "generalize_name",
    "register",
    "validate_config_model",
]

CONNECTOR_TYPES: dict[str, ConnectorFactory] = {}
"""Connector type name (``SourceType`` value) to factory."""

_BUILTIN_MODULES: Final = (
    "carto_edge.connectors.upload",
    "carto_edge.connectors.splunk",
    "carto_edge.connectors.sql",
    "carto_edge.connectors.sftp",
    "carto_edge.connectors.webhook",
)

_DIGITS: Final = re.compile(r"\d+")

F = TypeVar("F", bound=ConnectorFactory)


def register(type_name: str) -> Callable[[F], F]:
    """Class decorator: ``@register("splunk")`` on a :class:`ReadConnector` implementation."""
    if type_name not in {member.value for member in SourceType}:
        msg = f"unknown connector type {type_name!r}; add it to SourceType first"
        raise ValueError(msg)

    def decorate(factory: F) -> F:
        existing = CONNECTOR_TYPES.get(type_name)
        if existing is not None and existing is not factory:
            msg = f"connector type {type_name!r} is already registered"
            raise ValueError(msg)
        CONNECTOR_TYPES[type_name] = factory
        return factory

    return decorate


def summarize_validation_error(exc: ValidationError) -> str:
    """Locations and messages without the inputs (spec 14.3: a misplaced secret is not echoed)."""
    parts: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(piece) for piece in error.get("loc", ())) or "config"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts[:10])


def validate_config_model[C: ConnectorConfig](
    model: type[C], cfg: Mapping[str, Any], source_id: str
) -> C:
    """Validate a connector ``config`` block against its pydantic model."""
    try:
        return model.model_validate(dict(cfg))
    except ValidationError as exc:
        msg = f"source {source_id!r} config is invalid: {summarize_validation_error(exc)}"
        raise InvalidConfigError(msg) from exc


def generalize_name(name: str) -> str:
    """``SHIP_20261006_2130.csv`` to ``SHIP_*_*.csv`` (ADR 0016: digit runs to ``*``)."""
    return _DIGITS.sub("*", name)


def _load_builtin() -> None:
    for module_name in _BUILTIN_MODULES:
        importlib.import_module(module_name)


def build_connector(source: SourceConfig, context: ConnectorContext) -> ReadConnector:
    """Instantiate the connector for ``source``; raises :class:`InvalidConfigError` when the
    type is unknown (or not pollable, like ``otlp``) or the config block fails validation."""
    _load_builtin()
    factory = CONNECTOR_TYPES.get(source.type.value)
    if factory is None:
        msg = (
            f"source {source.id!r} has type {source.type.value!r} which has no connector "
            "(otlp sources are received by the gateway collector endpoint, not polled)"
        )
        raise InvalidConfigError(msg)
    try:
        return factory(source, context)
    except ValidationError as exc:
        msg = f"source {source.id!r} config is invalid: {summarize_validation_error(exc)}"
        raise InvalidConfigError(msg) from exc
