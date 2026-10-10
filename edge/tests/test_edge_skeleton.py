"""The edge packages import, and the CLI entry point is the real one (no M0 placeholder left)."""

import pytest

import carto_edge.cli.main as cli_main
import carto_edge.connectors
import carto_edge.gateway
import carto_edge.pipeline


def test_cli_without_a_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main.main([]) == 2
    assert "usage: carto-edge" in capsys.readouterr().err
    assert not hasattr(cli_main, "NOT_YET")


def test_subpackages_import() -> None:
    assert carto_edge.connectors.__doc__ and "No write methods" in carto_edge.connectors.__doc__
    assert carto_edge.gateway.__doc__
    assert carto_edge.pipeline.__doc__
