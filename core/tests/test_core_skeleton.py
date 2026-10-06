"""The core skeleton imports with every worker package present (spec 5.3)."""

import importlib

import pytest

WORKERS = [
    "ingest",
    "profiler",
    "linker",
    "assembler",
    "detector",
    "notifier",
    "api",
    "llm_gateway",
]


@pytest.mark.parametrize("name", WORKERS)
def test_core_subpackage_imports(name: str) -> None:
    module = importlib.import_module(f"carto_core.{name}")
    assert module.__doc__
