"""The pinned HTTP client for connectors (spec 14.7 SSRF pinning, 14.4 TLS; ADR 0018).

Every HTTP connection a connector opens goes through :func:`build_async_client`:

- :class:`PinnedAsyncTransport` is an ``httpx`` transport whose connection pool uses a custom
  ``httpcore`` network backend. ``httpcore`` asks the backend to open a TCP connection to the
  URL's host; the backend hands the name to the :class:`NetworkPolicy`, which resolves it,
  applies the spec 14.7 rules and returns one pinned address, and the socket is opened to that
  address. ``httpcore`` then starts TLS on the stream with ``server_hostname`` set to the
  original host name (it never sees the address), so certificate verification checks the name
  the admin configured, while DNS rebinding between validation and connect cannot redirect the
  socket (the address was decided once, by the policy).
- :func:`build_ssl_context` enforces TLS 1.2 as the minimum, loads a custom CA bundle and an
  optional client certificate, and only turns verification off when the caller passes the
  explicit admin flag (spec 8.1: "verification can't be disabled without an admin flag that is
  audited and shown as a warning"), which is logged as a warning here.
- Clients never follow redirects (a redirect could point at an address the policy refused) and
  never read proxy settings from the environment (a proxy would bypass the pinning).

Request and response bodies are never logged (spec 14.12).
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

import anyio
import httpcore
import httpx

from carto_edge.connectors.base import NetworkPolicy
from carto_edge.net.ssrf import SsrfError

__all__ = ["PinnedAsyncTransport", "build_async_client", "build_ssl_context"]

logger = logging.getLogger(__name__)

DEFAULT_LIMITS: Final = httpx.Limits(max_connections=10, max_keepalive_connections=5)
USER_AGENT: Final = "carto-edge"


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections to the address the network policy pinned for the host."""

    def __init__(
        self, policy: NetworkPolicy, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self._policy = policy
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            # The policy may do a blocking DNS lookup: keep it off the event loop.
            resolved = await anyio.to_thread.run_sync(self._policy.resolve, host, port)
        except SsrfError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        # TLS, if any, is started by httpcore on the returned stream with the original host as
        # server_hostname, so the certificate is still checked against the configured name.
        return await self._inner.connect_tcp(
            resolved.address,
            resolved.port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = (path, timeout, socket_options)
        msg = "unix sockets are not allowed for connector traffic"
        raise httpcore.ConnectError(msg)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedAsyncTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose pool connects through :class:`_PinnedBackend`.

    The parent class owns request translation, exception mapping and ``aclose``; only the
    connection pool is swapped for one with the pinned backend and the caller's SSL context.
    No proxy support on purpose: a proxy would make the connection on our behalf, unpinned.
    """

    def __init__(
        self,
        policy: NetworkPolicy,
        ssl_context: ssl.SSLContext,
        *,
        limits: httpx.Limits = DEFAULT_LIMITS,
        retries: int = 0,
    ) -> None:
        super().__init__(verify=ssl_context, trust_env=False, limits=limits, retries=retries)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            retries=retries,
            network_backend=_PinnedBackend(policy),
        )


def build_ssl_context(
    *,
    ca_file: Path | None,
    client_cert: tuple[Path, Path] | None,
    verify: bool,
) -> ssl.SSLContext:
    """A client context: TLS 1.2 minimum (spec 14.4), the system or custom CA bundle, an
    optional client certificate, and verification off only when ``verify`` is False."""
    if ca_file is not None and not ca_file.is_file():
        msg = f"ca_file {ca_file} is not a readable file"
        raise OSError(msg)
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=str(ca_file) if ca_file is not None else None
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client_cert is not None:
        certificate, key = client_cert
        context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def build_async_client(
    policy: NetworkPolicy,
    *,
    ca_file: Path | None,
    client_cert: tuple[Path, Path] | None,
    timeout: float | httpx.Timeout,
    verify: bool = True,
    base_url: str = "",
    headers: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    limits: httpx.Limits = DEFAULT_LIMITS,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` pinned by ``policy``.

    ``verify=False`` is the audited admin flag of spec 8.1: it is logged as a warning every
    time a client is built with it. ``transport`` exists for tests (``httpx.MockTransport``);
    production callers leave it unset so every connection is pinned.
    """
    if not verify:
        logger.warning(
            "TLS certificate verification disabled by admin flag for %s (spec 8.1)",
            base_url or "a connector client",
        )
    if transport is None:
        context = build_ssl_context(ca_file=ca_file, client_cert=client_cert, verify=verify)
        transport = PinnedAsyncTransport(policy, context, limits=limits)
    merged: dict[str, str] = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout),
        base_url=base_url,
        headers=merged,
        trust_env=False,
        follow_redirects=False,
        max_redirects=0,
    )
