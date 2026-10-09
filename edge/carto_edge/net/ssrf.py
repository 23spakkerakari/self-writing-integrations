"""SSRF validation with address pinning for connector hosts (spec 14.7, ADR 0018).

Spec 14.7: "resolve DNS and reject loopback, link-local (including ``169.254.169.254`` and IPv6
equivalents), the cluster's own service and pod ranges, and any private range not in the
admin's allowed-CIDR list (sources are usually internal, so private ranges are allowed only when
explicitly listed). Pin the resolved IP for the connection to defeat DNS rebinding."

:class:`DefaultNetworkPolicy` implements :class:`carto_edge.connectors.base.NetworkPolicy`:

- Always refused, whatever the allow-list: loopback, unspecified, multicast, reserved,
  link-local (``169.254.0.0/16`` with the cloud metadata address, ``fe80::/10``), the AWS IMDS
  IPv6 address ``fd00:ec2::254`` and the Alibaba metadata address ``100.100.100.200``. An
  IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is judged by its IPv4 form and pinned as IPv4.
- Refused unless inside an allowed CIDR: the private ranges ``10/8``, ``172.16/12``,
  ``192.168/16``, the shared address space ``100.64/10`` and ``fc00::/7`` (plus anything else
  the standard library calls private, such as the benchmarking and documentation ranges).
- A literal address is validated without DNS; a name is resolved (``socket.getaddrinfo`` by
  default, injectable for tests) and every address it resolves to must pass: one bad address
  refuses the whole name, so a rebinding server cannot slip a private address into the answer
  next to a public one. The first acceptable address is pinned in the returned
  :class:`ResolvedHost` and the connection is opened to that address while TLS, SSH or the
  database driver still verify the name.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from typing import Final

from carto_edge.connectors.base import ResolvedHost

__all__ = ["ALWAYS_DENIED", "PRIVATE_RANGES", "DefaultNetworkPolicy", "Resolver", "SsrfError"]

Resolver = Callable[[str, int], Sequence[str]]
"""``(host, port) -> addresses`` as text; raises :class:`OSError` when the name does not resolve."""

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ALWAYS_DENIED: Final[tuple[IPNetwork, ...]] = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, includes 169.254.169.254
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS over IPv6
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),  # "this" network
)
"""Refused whatever the allow-list says (on top of loopback, unspecified, multicast, reserved)."""

PRIVATE_RANGES: Final[tuple[IPNetwork, ...]] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # shared address space (CGNAT), used by some clusters
    ipaddress.ip_network("fc00::/7"),  # unique local addresses
)
"""Refused unless the admin listed a CIDR that contains the address (spec 14.7)."""

MAX_HOST_NAME_LEN: Final = 253
_HOST_NAME_RE: Final = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


class SsrfError(Exception):
    """The host was refused by the network policy (spec 14.7). The message names the host and
    the offending address so an admin can extend the allow-list on purpose."""


def _getaddrinfo_resolver(host: str, port: int) -> Sequence[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.append(address)
    return seen


def _parse_literal(text: str) -> IPAddress | None:
    candidate = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    if "%" in candidate:  # scope ids name an interface: never a source host
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


class DefaultNetworkPolicy:
    """Spec 14.7 rules with an admin allow-list of private CIDRs (``network.allowed_source_cidrs``
    in the sources file)."""

    __slots__ = ("_allowed", "_resolver")

    def __init__(self, allowed_cidrs: Sequence[str], *, resolver: Resolver | None = None) -> None:
        allowed: list[IPNetwork] = []
        for cidr in allowed_cidrs:
            try:
                allowed.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError as exc:
                msg = f"allowed CIDR {cidr!r} is not a valid network"
                raise SsrfError(msg) from exc
        self._allowed: tuple[IPNetwork, ...] = tuple(allowed)
        self._resolver: Resolver = resolver if resolver is not None else _getaddrinfo_resolver

    def __repr__(self) -> str:
        return f"DefaultNetworkPolicy(allowed={[str(net) for net in self._allowed]})"

    # -- rules --------------------------------------------------------------------------------

    def rejection_reason(self, address: IPAddress) -> str | None:
        """Why ``address`` is refused, or ``None`` when a connector may open a socket to it."""
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        candidate: IPAddress = mapped if mapped is not None else address
        if candidate.is_loopback:
            return "loopback"
        if candidate.is_unspecified:
            return "unspecified"
        if candidate.is_multicast:
            return "multicast"
        if candidate.is_link_local:
            return "link-local"
        if any(candidate in network for network in ALWAYS_DENIED):
            return "metadata or link-local"
        if candidate.is_reserved:
            return "reserved"
        if any(candidate in network for network in self._allowed):
            return None
        if candidate.is_private or any(candidate in network for network in PRIVATE_RANGES):
            return "private range not in network.allowed_source_cidrs"
        return None

    # -- resolution ---------------------------------------------------------------------------

    def resolve(self, host: str, port: int) -> ResolvedHost:
        if isinstance(port, bool) or not 1 <= port <= 65535:
            msg = f"port {port!r} is out of range for host {host!r}"
            raise SsrfError(msg)
        literal = _parse_literal(host)
        if literal is not None:
            self._check(host, literal)
            shown = host[1:-1] if host.startswith("[") else host
            return ResolvedHost(host=shown, address=_pin_text(literal), port=port)
        if not host or len(host) > MAX_HOST_NAME_LEN or not _HOST_NAME_RE.match(host):
            msg = f"host {host!r} is not a valid host name or address"
            raise SsrfError(msg)
        try:
            addresses = list(self._resolver(host, port))
        except OSError as exc:
            msg = f"host {host!r} did not resolve"
            raise SsrfError(msg) from exc
        if not addresses:
            msg = f"host {host!r} did not resolve to any address"
            raise SsrfError(msg)
        pinned: str | None = None
        for text in addresses:
            parsed = _parse_literal(text)
            if parsed is None:
                msg = f"host {host!r} resolved to an unparseable address"
                raise SsrfError(msg)
            self._check(host, parsed)
            if pinned is None:
                pinned = _pin_text(parsed)
        assert pinned is not None  # noqa: S101 - addresses is non-empty and every one passed
        return ResolvedHost(host=host, address=pinned, port=port)

    def _check(self, host: str, address: IPAddress) -> None:
        reason = self.rejection_reason(address)
        if reason is not None:
            msg = f"host {host!r} resolves to {address} which is refused: {reason} (spec 14.7)"
            raise SsrfError(msg)


def _pin_text(address: IPAddress) -> str:
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return str(mapped) if mapped is not None else str(address)
