"""carto_edge.gateway.server (spec 12 "Internal", 14.4, 8.5; plan M1 wave 2 D2): the gateway
serves mutual TLS only with TLS 1.2 as the floor and uvicorn's own logging off, refuses to start
without a valid sources file, listener files or keys (before any store is opened), and its
housekeeping loop seals stale batches every tick and persists statistics on its own period."""

from __future__ import annotations

import asyncio
import io
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

from carto_common.logging import configure_logging
from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_edge.config import EdgeSettings, GatewaySettings, PiiSettings
from carto_edge.gateway import server
from carto_edge.gateway.server import (
    EXIT_FAILURE,
    EXIT_USAGE,
    GatewayConfigError,
    build_uvicorn_config,
    check_listener_files,
    harden_tls_context,
    run_gateway,
    run_housekeeping,
)

SOURCES_YAML = """\
systems:
  - id: sys_web
    name: Webstore
sources:
  - id: src_web_log
    system: sys_web
    type: otlp
"""


@dataclass(frozen=True)
class Pki:
    ca: Path
    cert: Path
    key: Path


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    root = tmp_path_factory.mktemp("gateway-pki")
    ca = create_ca("carto test CA")
    write_pem(ca, root / "ca.crt", root / "ca.key")
    leaf = issue_certificate(
        ca, "edge-gateway", dns_names=("localhost",), ip_addresses=("127.0.0.1",)
    )
    write_pem(leaf, root / "edge-gateway.crt", root / "edge-gateway.key")
    return Pki(root / "ca.crt", root / "edge-gateway.crt", root / "edge-gateway.key")


def listener(pki: Pki, **overrides: object) -> GatewaySettings:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8443,
        "tls_cert_file": pki.cert,
        "tls_key_file": pki.key,
        "tls_client_ca_file": pki.ca,
    }
    return GatewaySettings.model_validate(values | overrides)


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    stream = io.StringIO()

    def configure(service: str) -> None:
        configure_logging(service, stream=stream)

    monkeypatch.setattr(server, "configure_logging", configure)
    return stream


def edge_settings(tmp_path: Path, **overrides: object) -> EdgeSettings:
    values: dict[str, object] = {
        "state_dir": tmp_path / "state",
        "pii": PiiSettings(enabled=False),
    }
    return EdgeSettings.model_validate(values | overrides)


# ---------------------------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("setting", ["tls_cert_file", "tls_key_file", "tls_client_ca_file"])
def test_every_listener_file_is_required(pki: Pki, tmp_path: Path, setting: str) -> None:
    with pytest.raises(GatewayConfigError, match=f"gateway.{setting} is required"):
        check_listener_files(listener(pki, **{setting: None}))
    with pytest.raises(GatewayConfigError, match="does not exist"):
        check_listener_files(listener(pki, **{setting: tmp_path / "missing.pem"}))


def test_listener_files_are_returned_when_present(pki: Pki) -> None:
    assert check_listener_files(listener(pki)) == (pki.cert, pki.key, pki.ca)


def test_uvicorn_config_serves_mutual_tls_with_a_tls_1_2_floor(pki: Pki) -> None:
    config = build_uvicorn_config(FastAPI(), listener(pki, port=9443), log_level="warning")
    assert config.host == "127.0.0.1"
    assert config.port == 9443
    assert config.ssl_certfile == str(pki.cert)
    assert config.ssl_keyfile == str(pki.key)
    assert config.ssl_ca_certs == str(pki.ca)
    assert config.ssl_cert_reqs == ssl.CERT_REQUIRED
    assert config.ssl_context_factory is harden_tls_context
    assert config.log_config is None
    assert config.access_log is False  # the app writes its own access line (spec 14.12)
    assert config.server_header is False
    assert config.proxy_headers is False
    assert config.lifespan == "on"
    config.load()
    assert config.ssl is not None
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    assert config.ssl.verify_mode == ssl.CERT_REQUIRED


def test_uvicorn_config_refuses_a_listener_without_tls() -> None:
    with pytest.raises(GatewayConfigError):
        build_uvicorn_config(FastAPI(), GatewaySettings())


def test_harden_tls_context_raises_the_floor(pki: Pki) -> None:
    config = build_uvicorn_config(FastAPI(), listener(pki))
    context = harden_tls_context(config, lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


# ---------------------------------------------------------------------------------------------
# run_gateway refusals (all before the buffer, cursors or any D1 stage exist)
# ---------------------------------------------------------------------------------------------


def test_run_gateway_needs_a_sources_file(tmp_path: Path, logs: io.StringIO) -> None:
    assert run_gateway(edge_settings(tmp_path)) == EXIT_USAGE == 2
    assert "gateway.sources_file_missing" in logs.getvalue()


@pytest.mark.parametrize(
    "text", ["systems: [1", "systems: []\nsources: []\nunknown_key: 1\n", "- just a list\n"]
)
def test_run_gateway_refuses_an_invalid_sources_file(
    tmp_path: Path, logs: io.StringIO, text: str
) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    assert run_gateway(edge_settings(tmp_path, sources_file=path)) == EXIT_USAGE
    assert "gateway.sources_file_invalid" in logs.getvalue()


def test_run_gateway_refuses_a_missing_sources_file(tmp_path: Path, logs: io.StringIO) -> None:
    settings = edge_settings(tmp_path, sources_file=tmp_path / "nowhere.yaml")
    assert run_gateway(settings) == EXIT_USAGE


def test_run_gateway_refuses_missing_listener_files(tmp_path: Path, logs: io.StringIO) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(SOURCES_YAML, encoding="utf-8")
    assert run_gateway(edge_settings(tmp_path, sources_file=path)) == EXIT_USAGE
    assert "gateway.listener_invalid" in logs.getvalue()
    assert not (tmp_path / "state").exists()


def test_run_gateway_without_keys_fails_before_opening_any_store(
    tmp_path: Path, pki: Pki, logs: io.StringIO
) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(SOURCES_YAML, encoding="utf-8")
    settings = edge_settings(tmp_path, sources_file=path, gateway=listener(pki))
    assert run_gateway(settings) == EXIT_FAILURE == 1
    assert "gateway.keys_unavailable" in logs.getvalue()
    assert not settings.buffer_db_file.exists()
    assert not settings.cursors_db_file.exists()
    assert not settings.vault_db_file.exists()


# ---------------------------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------------------------


async def test_housekeeping_seals_stale_batches_and_flushes_statistics() -> None:
    stop = asyncio.Event()
    stale: list[datetime] = []
    flushes: list[int] = []

    def flush_stale(now: datetime) -> int:
        stale.append(now)
        if len(stale) == 2:
            raise RuntimeError("one bad tick does not stop the loop")
        return 0

    def flush_runtime() -> None:
        flushes.append(1)

    task = asyncio.create_task(
        run_housekeeping(
            stop, flush_stale, flush_runtime, stats_flush_seconds=0.05, interval_seconds=0.01
        )
    )
    for _ in range(500):
        if len(stale) >= 8 and flushes:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(stale) >= 8
    assert all(moment.tzinfo is UTC for moment in stale)
    assert 1 <= len(flushes) < len(stale)


async def test_housekeeping_returns_promptly_when_stopped() -> None:
    stop = asyncio.Event()
    stop.set()
    calls: list[datetime] = []
    await asyncio.wait_for(
        run_housekeeping(stop, calls.append, lambda: None, stats_flush_seconds=30.0), timeout=2
    )
    assert calls == []
