"""The edge skeleton imports and its CLI refuses to pretend it works."""

import pytest

import carto_edge.connectors
import carto_edge.gateway
import carto_edge.pipeline
from carto_edge.cli.main import NOT_YET, main


def test_cli_returns_nonzero_until_m1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert NOT_YET in capsys.readouterr().out


def test_subpackages_import() -> None:
    assert carto_edge.connectors.__doc__ and "No write methods" in carto_edge.connectors.__doc__
    assert carto_edge.gateway.__doc__
    assert carto_edge.pipeline.__doc__
