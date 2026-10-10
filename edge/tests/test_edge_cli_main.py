"""carto-edge entry point: the four commands parse, --help works, audit verify reports an intact
or broken chain with the right exit code, and gateway hands the environment settings to the
gateway server (loaded lazily, so the other commands never import its stack)."""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from carto_edge.audit import EdgeAudit
from carto_edge.cli.main import GATEWAY_MODULE, build_parser, main
from carto_edge.config import EdgeSettings


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configured, config = structlog.is_configured(), structlog.get_config()
    yield
    if configured:
        structlog.configure(**config)
    else:
        structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for command in ("analyze", "gateway", "bench", "audit"):
        assert command in out


@pytest.mark.parametrize("command", ["analyze", "bench", "gateway", "audit"])
def test_help_of_each_command(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([command, "--help"]) == 0
    assert "usage: carto-edge" in capsys.readouterr().out


def test_no_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: carto-edge" in capsys.readouterr().err
    assert main(["analyze"]) == 2  # required options missing
    assert main(["audit"]) == 2
    assert main(["frobnicate"]) == 2


def test_parser_matches_the_makefile_flags() -> None:
    args = build_parser().parse_args(
        [
            "analyze",
            "--config",
            "simulator/analyze.shop.yaml",
            "--input",
            "sim-out/shop",
            "--out",
            "sim-out/shop.carto",
            "--state-dir",
            "sim-out/shop.edge-state",
            "--locator-map",
        ]
    )
    assert args.locator_map is True
    assert args.state_dir == Path("sim-out/shop.edge-state")
    bench_args = build_parser().parse_args(
        ["bench", "--input", "sim-out/shop", "--config", "c.yaml", "--seconds", "30"]
    )
    assert bench_args.seconds == 30.0
    assert bench_args.max_records == 50_000


def audit_file(tmp_path: Path) -> Path:
    path = tmp_path / "audit.ndjson"
    with EdgeAudit(path) as audit:
        audit.record("reveal", "user-1", "token", {"count": 1})
        audit.record("reveal", "user-2", "token", {"count": 2})
    return path


def test_audit_verify_intact_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = audit_file(tmp_path)
    assert main(["audit", "verify", "--file", str(path)]) == 0
    assert "2 rows, intact" in capsys.readouterr().out


def test_audit_verify_broken_chain_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = audit_file(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(lines[0].replace("user-1", "user-9") + lines[1], encoding="utf-8")
    assert main(["audit", "verify", "--file", str(path)]) == 1
    assert "BROKEN at line 1" in capsys.readouterr().out


def test_audit_verify_missing_file_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audit", "verify", "--file", str(tmp_path / "none.ndjson")]) == 2
    assert "not a file" in capsys.readouterr().err


def test_gateway_runs_the_server_with_environment_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[EdgeSettings] = []

    def run_gateway(settings: EdgeSettings) -> int:
        seen.append(settings)
        return 0

    fake = types.ModuleType(GATEWAY_MODULE)
    fake.run_gateway = run_gateway  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, GATEWAY_MODULE, fake)
    monkeypatch.setenv("CARTO_STATE_DIR", str(tmp_path / "state"))
    assert main(["gateway"]) == 0
    assert len(seen) == 1
    assert seen[0].state_dir == tmp_path / "state"


def test_gateway_unavailable_or_misconfigured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, GATEWAY_MODULE, None)  # import raises ImportError
    assert main(["gateway"]) == 1
    assert "not available" in capsys.readouterr().err
    fake = types.ModuleType(GATEWAY_MODULE)
    fake.run_gateway = lambda _settings: 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, GATEWAY_MODULE, fake)
    monkeypatch.setenv("CARTO_TENANT_ID", "Not A Tenant!")
    assert main(["gateway"]) == 2
    assert "invalid settings" in capsys.readouterr().err
