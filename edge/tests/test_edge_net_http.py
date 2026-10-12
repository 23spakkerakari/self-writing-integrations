"""carto_edge.net.http: the pinned transport connects to the policy's address while TLS still
verifies the configured host name (spec 14.7 "Pin the resolved IP", 14.4 TLS 1.2 minimum, 8.1
"verification can't be disabled without an admin flag"; ADR 0018).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_edge.connectors.base import NetworkPolicy, ResolvedHost
from carto_edge.net.http import PinnedAsyncTransport, build_async_client, build_ssl_context
from carto_edge.net.ssrf import SsrfError

HOST = "source.internal.example"


class MappingPolicy:
    """A policy that pins every name to 127.0.0.1 and refuses one name, for the tests."""

    def __init__(self, refuse: str = "refused.example") -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> ResolvedHost:
        self.calls.append((host, port))
        if host == self.refuse:
            msg = f"host {host!r} refused"
            raise SsrfError(msg)
        return ResolvedHost(host=host, address="127.0.0.1", port=port)


assert isinstance(MappingPolicy(), NetworkPolicy)


class Server:
    """A one-request HTTP/1.1 responder that records the request head it saw."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self.ssl_context = ssl_context
        self.heads: list[bytes] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        self.heads.append(head)
        body = b'{"ok": true}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            await writer.wait_closed()

    async def __aenter__(self) -> Server:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self.ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
async def plain_server() -> AsyncIterator[Server]:
    async with Server() as server:
        yield server


@pytest.fixture
def pki(tmp_path: Path) -> dict[str, Path]:
    ca = create_ca("test CA")
    leaf = issue_certificate(ca, HOST, dns_names=(HOST,), server=True, client=False)
    write_pem(ca, tmp_path / "ca.pem", tmp_path / "ca.key")
    write_pem(leaf, tmp_path / "leaf.pem", tmp_path / "leaf.key")
    return {"ca": tmp_path / "ca.pem", "cert": tmp_path / "leaf.pem", "key": tmp_path / "leaf.key"}


@pytest.fixture
async def tls_server(pki: dict[str, Path]) -> AsyncIterator[Server]:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(pki["cert"], pki["key"])
    async with Server(context) as server:
        yield server


# ---------------------------------------------------------------------------------------------


async def test_plain_http_connects_to_pinned_address_with_original_host_header(
    plain_server: Server,
) -> None:
    policy = MappingPolicy()
    async with build_async_client(policy, ca_file=None, client_cert=None, timeout=5.0) as client:
        response = await client.get(f"http://{HOST}:{plain_server.port}/services/ping")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert policy.calls == [(HOST, plain_server.port)]
    assert b"Host: " + HOST.encode() in plain_server.heads[0]
    assert b"GET /services/ping HTTP/1.1" in plain_server.heads[0]


async def test_refused_host_never_opens_a_socket(plain_server: Server) -> None:
    policy = MappingPolicy(refuse=HOST)
    async with build_async_client(policy, ca_file=None, client_cert=None, timeout=5.0) as client:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await client.get(f"http://{HOST}:{plain_server.port}/")
    assert plain_server.heads == []


async def test_tls_verifies_the_name_against_the_pinned_address(
    tls_server: Server, pki: dict[str, Path]
) -> None:
    policy = MappingPolicy()
    async with build_async_client(
        policy, ca_file=pki["ca"], client_cert=None, timeout=5.0
    ) as client:
        response = await client.get(f"https://{HOST}:{tls_server.port}/")
    assert response.status_code == 200
    assert b"Host: " + HOST.encode() in tls_server.heads[0]


async def test_tls_rejects_a_name_the_certificate_does_not_cover(
    tls_server: Server, pki: dict[str, Path]
) -> None:
    policy = MappingPolicy()
    async with build_async_client(
        policy, ca_file=pki["ca"], client_cert=None, timeout=5.0
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://other.internal.example:{tls_server.port}/")


async def test_tls_rejects_an_unknown_ca(tls_server: Server) -> None:
    policy = MappingPolicy()
    async with build_async_client(policy, ca_file=None, client_cert=None, timeout=5.0) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://{HOST}:{tls_server.port}/")


async def test_verify_false_needs_the_admin_flag_and_warns(
    tls_server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    policy = MappingPolicy()
    with caplog.at_level(logging.WARNING, logger="carto_edge.net.http"):
        client = build_async_client(
            policy, ca_file=None, client_cert=None, timeout=5.0, verify=False
        )
    assert any("verification disabled" in record.getMessage() for record in caplog.records)
    async with client:
        response = await client.get(f"https://{HOST}:{tls_server.port}/")
    assert response.status_code == 200


def test_ssl_context_minimum_tls_1_2(pki: dict[str, Path]) -> None:
    context = build_ssl_context(ca_file=pki["ca"], client_cert=None, verify=True)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ssl_context_loads_client_certificate(pki: dict[str, Path]) -> None:
    context = build_ssl_context(
        ca_file=pki["ca"], client_cert=(pki["cert"], pki["key"]), verify=True
    )
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_ssl_context_missing_ca_file(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="ca"):
        build_ssl_context(ca_file=tmp_path / "missing.pem", client_cert=None, verify=True)


async def test_transport_is_an_httpx_transport() -> None:
    transport = PinnedAsyncTransport(
        MappingPolicy(), build_ssl_context(ca_file=None, client_cert=None, verify=True)
    )
    assert isinstance(transport, httpx.AsyncBaseTransport)
    await transport.aclose()


async def test_client_accepts_a_test_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    async with build_async_client(
        MappingPolicy(),
        ca_file=None,
        client_cert=None,
        timeout=1.0,
        transport=httpx.MockTransport(handler),
        base_url="https://splunk.example:8089",
    ) as client:
        assert (await client.get("/x")).status_code == 204
