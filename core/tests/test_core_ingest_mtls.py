"""ingest-api over mutual TLS (spec 12 "Internal", 14.4): a throwaway CA from carto_common.pki,
uvicorn in a thread on a free port; a valid client certificate succeeds, no certificate or a
certificate from another CA is refused at the handshake, plain HTTP and TLS below 1.2 never
connect."""

from __future__ import annotations

import socket
import ssl
import threading
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn

from carto_common.ids import new_ulid
from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_core.db.clickhouse import WriteResult
from carto_core.ingest.app import create_app
from carto_core.ingest.health import InMemorySourceHealthStore
from carto_core.ingest.ledger import InMemoryBatchLedger
from carto_core.ingest.server import build_uvicorn_config, harden_tls_context
from carto_core.settings import CoreConfigError, CoreSettings, IngestListenerSettings
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch

JSON = {"content-type": "application/json"}


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[list[CanonicalEvent]] = []

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        self.batches.append(list(events))
        return WriteResult(len(events), 0)

    def ping(self) -> bool:
        return True


@dataclass(frozen=True)
class Pki:
    ca_cert: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    stranger_cert: Path
    stranger_key: Path


@dataclass
class Served:
    url: str
    host: str
    port: int
    writer: FakeWriter


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    root = tmp_path_factory.mktemp("pki")
    ca = create_ca("carto test CA")
    write_pem(ca, root / "ca.crt", root / "ca.key")
    server = issue_certificate(
        ca, "ingest-api", dns_names=("localhost",), ip_addresses=("127.0.0.1",), client=False
    )
    write_pem(server, root / "ingest-api.crt", root / "ingest-api.key")
    client = issue_certificate(ca, "edge-gateway", server=False)
    write_pem(client, root / "edge-gateway.crt", root / "edge-gateway.key")
    stranger_ca = create_ca("someone else's CA")
    stranger = issue_certificate(stranger_ca, "stranger", server=False)
    write_pem(stranger, root / "stranger.crt", root / "stranger.key")
    return Pki(
        ca_cert=root / "ca.crt",
        server_cert=root / "ingest-api.crt",
        server_key=root / "ingest-api.key",
        client_cert=root / "edge-gateway.crt",
        client_key=root / "edge-gateway.key",
        stranger_cert=root / "stranger.crt",
        stranger_key=root / "stranger.key",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _listener(pki: Pki, port: int, **overrides: object) -> IngestListenerSettings:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": port,
        "tls_cert_file": pki.server_cert,
        "tls_key_file": pki.server_key,
        "client_ca_file": pki.ca_cert,
    }
    return IngestListenerSettings.model_validate(values | overrides)


@pytest.fixture(scope="module")
def served(pki: Pki) -> Iterator[Served]:
    port = _free_port()
    settings = CoreSettings(ingest=_listener(pki, port))
    writer = FakeWriter()
    app = create_app(settings, writer, InMemoryBatchLedger(), InMemorySourceHealthStore())
    server = uvicorn.Server(build_uvicorn_config(app, settings.ingest))
    thread = threading.Thread(target=server.run, name="ingest-api-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("uvicorn did not start")
        time.sleep(0.05)
    yield Served(f"https://127.0.0.1:{port}", "127.0.0.1", port, writer)
    server.should_exit = True
    thread.join(timeout=10)


def _context(pki: Pki, certificate: tuple[Path, Path] | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(pki.ca_cert))
    if certificate is not None:
        context.load_cert_chain(str(certificate[0]), str(certificate[1]))
    return context


def _batch_body() -> bytes:
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=datetime.now(UTC),
        events=[CanonicalEvent.example()],
    )
    return batch.model_dump_json().encode("utf-8")


def test_client_with_a_certificate_from_the_ca_is_served(served: Served, pki: Pki) -> None:
    with httpx.Client(verify=_context(pki, (pki.client_cert, pki.client_key))) as client:
        health = client.get(f"{served.url}/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert "server" not in health.headers, "no server banner"
        accepted = client.post(f"{served.url}/internal/ingest", content=_batch_body(), headers=JSON)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"accepted": 1, "duplicate": False}
    assert len(served.writer.batches) == 1


def test_without_a_client_certificate_the_handshake_is_refused(served: Served, pki: Pki) -> None:
    with (
        httpx.Client(verify=_context(pki, None), timeout=5) as client,
        pytest.raises(httpx.HTTPError),
    ):
        client.get(f"{served.url}/healthz")


def test_a_certificate_from_another_ca_is_refused(served: Served, pki: Pki) -> None:
    with (
        httpx.Client(
            verify=_context(pki, (pki.stranger_cert, pki.stranger_key)), timeout=5
        ) as client,
        pytest.raises(httpx.HTTPError),
    ):
        client.get(f"{served.url}/healthz")


def test_plain_http_is_not_served(served: Served) -> None:
    with httpx.Client(timeout=5) as client, pytest.raises(httpx.HTTPError):
        client.get(f"http://{served.host}:{served.port}/healthz")


def test_tls_below_1_2_is_never_negotiated(served: Served, pki: Pki) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(str(pki.ca_cert))
    context.load_cert_chain(str(pki.client_cert), str(pki.client_key))
    try:
        with warnings.catch_warnings():
            # Offering TLS 1.1 is deprecated, which is the point of this test.
            warnings.simplefilter("ignore", DeprecationWarning)
            context.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ValueError, ssl.SSLError):
        pytest.skip("this platform cannot offer TLS 1.1 at all")
    with (
        pytest.raises((ssl.SSLError, OSError)),
        socket.create_connection((served.host, served.port), timeout=5) as sock,
        context.wrap_socket(sock, server_hostname="127.0.0.1") as tls,
    ):
        tls.do_handshake()


def test_server_context_has_tls_1_2_as_the_floor_and_requires_client_certs(pki: Pki) -> None:
    settings = CoreSettings(ingest=_listener(pki, _free_port()))
    app = create_app(settings, FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore())
    config = build_uvicorn_config(app, settings.ingest)
    config.load()
    assert config.ssl is not None
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    assert config.ssl.verify_mode == ssl.CERT_REQUIRED
    assert config.log_config is None
    assert config.server_header is False
    assert config.proxy_headers is False


def test_harden_tls_context_only_raises_the_floor() -> None:
    def default() -> ssl.SSLContext:
        return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    context = harden_tls_context(uvicorn.Config(app="x:y"), default)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED


def test_missing_tls_files_are_refused_before_listening(pki: Pki, tmp_path: Path) -> None:
    app = create_app(
        CoreSettings(), FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore()
    )
    with pytest.raises(CoreConfigError, match="tls_cert_file"):
        build_uvicorn_config(app, IngestListenerSettings())
    with pytest.raises(CoreConfigError, match="client_ca_file"):
        build_uvicorn_config(
            app,
            IngestListenerSettings(tls_cert_file=pki.server_cert, tls_key_file=pki.server_key),
        )
    with pytest.raises(CoreConfigError, match="tls_key_file"):
        build_uvicorn_config(
            app,
            IngestListenerSettings(
                tls_cert_file=pki.server_cert,
                tls_key_file=tmp_path / "absent.key",
                client_ca_file=pki.ca_cert,
            ),
        )


def test_client_certificate_can_only_be_relaxed_explicitly(pki: Pki) -> None:
    app = create_app(
        CoreSettings(), FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore()
    )
    config = build_uvicorn_config(
        app, _listener(pki, _free_port(), require_client_cert=False, client_ca_file=None)
    )
    assert config.ssl_cert_reqs == ssl.CERT_NONE
