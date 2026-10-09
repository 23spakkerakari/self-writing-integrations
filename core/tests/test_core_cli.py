"""``carto-core`` argument handling and exit codes: usage errors are 2 and never raise, failures
to reach a store are 1 with a message that names the cause and never a credential, and the
ingest-api command refuses to start without its TLS files before touching any database."""

from __future__ import annotations

import os
import sys

import pytest

from carto_core.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, build_parser, main


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("CARTO_"):
            monkeypatch.delenv(name)
    # Unreachable local ports so a store connection fails at once, never a real server.
    monkeypatch.setenv("CARTO_CLICKHOUSE__URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CARTO_CLICKHOUSE__SECURE", "false")
    monkeypatch.setenv("CARTO_CLICKHOUSE__TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("CARTO_POSTGRES__PORT", "1")
    monkeypatch.setenv("CARTO_POSTGRES__SSLMODE", "disable")
    monkeypatch.setenv("CARTO_POSTGRES__CONNECT_TIMEOUT_SECONDS", "1")


@pytest.mark.parametrize(
    "argv",
    [[], ["frobnicate"], ["verify-bundle"], ["load-bundle"], ["migrate", "--bogus"]],
)
def test_usage_errors_return_2_without_raising(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        code = main(argv)
    except SystemExit as exc:
        pytest.fail(f"main({argv!r}) raised SystemExit({exc.code!r})")
    assert code == EXIT_USAGE
    assert capsys.readouterr().out == ""


def test_help_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for command in ("migrate", "ingest-api", "verify-bundle", "load-bundle"):
        assert command in out


def test_migrate_scopes_are_exclusive() -> None:
    parser = build_parser()
    args = parser.parse_args(["migrate", "--clickhouse-only"])
    assert args.clickhouse_only and not args.postgres_only
    with pytest.raises(SystemExit):
        parser.parse_args(["migrate", "--clickhouse-only", "--postgres-only"])


def test_chunk_size_is_validated_before_anything_runs(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["load-bundle", "--bundle", "nowhere.carto", "--chunk-size", "0"]) == EXIT_USAGE
    assert "--chunk-size" in capsys.readouterr().err
    assert main(["load-bundle", "--bundle", "nowhere.carto", "--chunk-size", "6000"]) == EXIT_USAGE


def test_invalid_settings_are_a_usage_error_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARTO_POSTGRES__PASSWORD", "hunter2-in-the-environment")
    assert main(["migrate"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "invalid settings" in err
    assert "hunter2" not in err


def test_unreachable_clickhouse_is_a_failure_naming_the_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["migrate", "--clickhouse-only"]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "migration failed" in err
    assert "cannot connect to ClickHouse" in err


def test_unreachable_postgres_is_a_failure_naming_the_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["migrate", "--postgres-only"]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "migration failed" in err
    assert "OperationalError" in err


def test_ingest_api_refuses_to_start_without_tls_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ingest-api"]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "ingest-api cannot start" in err
    assert "tls_cert_file" in err


def test_module_entry_point_matches_main() -> None:
    assert sys.modules["carto_core.cli"].main is main
