"""carto_edge.net.ssrf: the spec 14.7 SSRF suite (spec 18.3 "SSRF suite", ADR 0018).

Loopback, unspecified, multicast, reserved, link-local (the cloud metadata addresses among
them), IPv4-mapped IPv6 forms of those, and private ranges outside the admin's allow-list are
refused; a name that resolves to any refused address is refused entirely; the accepted address
is pinned.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from carto_edge.connectors.base import NetworkPolicy, ResolvedHost
from carto_edge.net.ssrf import DefaultNetworkPolicy, SsrfError


def resolver_for(table: dict[str, Sequence[str]]) -> Callable[[str, int], Sequence[str]]:
    def resolve(host: str, port: int) -> Sequence[str]:
        assert port > 0
        try:
            return table[host]
        except KeyError as exc:
            raise OSError("name not found") from exc

    return resolve


def test_policy_is_a_network_policy() -> None:
    assert isinstance(DefaultNetworkPolicy([]), NetworkPolicy)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.9.9.9",
        "0.0.0.0",  # noqa: S104 - the point of the test
        "169.254.169.254",
        "169.254.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fd00:ec2::254",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:0.0.0.0",
    ],
)
def test_always_rejected_literals(address: str) -> None:
    policy = DefaultNetworkPolicy(["0.0.0.0/0", "::/0"])  # even a blanket allow-list
    with pytest.raises(SsrfError):
        policy.resolve(address, 443)


@pytest.mark.parametrize(
    "address",
    ["10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.1", "100.64.0.1", "fc00::1", "fdab::1"],
)
def test_private_rejected_without_allow_list(address: str) -> None:
    with pytest.raises(SsrfError):
        DefaultNetworkPolicy([]).resolve(address, 8089)


@pytest.mark.parametrize(
    ("address", "cidr"),
    [
        ("10.20.1.5", "10.20.0.0/16"),
        ("172.16.5.5", "172.16.0.0/12"),
        ("192.168.1.1", "192.168.1.0/24"),
        ("100.64.3.3", "100.64.0.0/10"),
        ("fdab::1", "fdab::/64"),
        ("::ffff:10.20.1.5", "10.20.0.0/16"),
    ],
)
def test_private_allowed_inside_listed_cidr(address: str, cidr: str) -> None:
    resolved = DefaultNetworkPolicy([cidr]).resolve(address, 5432)
    assert isinstance(resolved, ResolvedHost)
    assert resolved.host == address
    assert resolved.port == 5432
    # An IPv4-mapped address is pinned in its IPv4 form.
    assert resolved.address in {address, address.removeprefix("::ffff:")}


def test_private_outside_listed_cidr_rejected() -> None:
    with pytest.raises(SsrfError):
        DefaultNetworkPolicy(["10.20.0.0/16"]).resolve("10.21.0.1", 5432)


def test_public_address_accepted_without_allow_list() -> None:
    resolved = DefaultNetworkPolicy([]).resolve("93.184.216.34", 443)
    assert resolved.address == "93.184.216.34"


def test_bracketed_ipv6_literal() -> None:
    resolved = DefaultNetworkPolicy([]).resolve("[2606:4700::1111]", 443)
    assert resolved.address == "2606:4700::1111"
    assert resolved.host == "2606:4700::1111"


def test_name_pins_first_acceptable_address() -> None:
    policy = DefaultNetworkPolicy(
        ["10.20.0.0/16"], resolver=resolver_for({"splunk.internal": ["10.20.0.9", "10.20.0.10"]})
    )
    resolved = policy.resolve("splunk.internal", 8089)
    assert resolved == ResolvedHost(host="splunk.internal", address="10.20.0.9", port=8089)


def test_name_with_any_rejected_address_is_refused_entirely() -> None:
    policy = DefaultNetworkPolicy(
        ["10.20.0.0/16"],
        resolver=resolver_for({"rebind.example": ["10.20.0.9", "169.254.169.254"]}),
    )
    with pytest.raises(SsrfError, match=r"rebind\.example"):
        policy.resolve("rebind.example", 443)


def test_name_resolving_to_loopback_is_refused() -> None:
    policy = DefaultNetworkPolicy([], resolver=resolver_for({"localhost": ["127.0.0.1", "::1"]}))
    with pytest.raises(SsrfError):
        policy.resolve("localhost", 443)


def test_name_that_does_not_resolve() -> None:
    policy = DefaultNetworkPolicy([], resolver=resolver_for({}))
    with pytest.raises(SsrfError, match="resolve"):
        policy.resolve("nowhere.example", 443)


def test_name_with_no_addresses() -> None:
    policy = DefaultNetworkPolicy([], resolver=resolver_for({"empty.example": []}))
    with pytest.raises(SsrfError):
        policy.resolve("empty.example", 443)


@pytest.mark.parametrize(
    "host",
    ["", "user@host.example", "host.example/path", "host example", "-bad.example", "a" * 254],
)
def test_malformed_host_names(host: str) -> None:
    policy = DefaultNetworkPolicy([], resolver=resolver_for({}))
    with pytest.raises(SsrfError):
        policy.resolve(host, 443)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_bad_ports(port: int) -> None:
    with pytest.raises(SsrfError):
        DefaultNetworkPolicy([]).resolve("93.184.216.34", port)


def test_invalid_allowed_cidr_is_rejected_at_construction() -> None:
    with pytest.raises(SsrfError):
        DefaultNetworkPolicy(["10.0.0.0/33"])
    with pytest.raises(SsrfError):
        DefaultNetworkPolicy(["not a cidr"])


def test_error_messages_name_the_host_but_not_the_resolver_internals() -> None:
    policy = DefaultNetworkPolicy([], resolver=resolver_for({"meta.example": ["169.254.169.254"]}))
    with pytest.raises(SsrfError) as info:
        policy.resolve("meta.example", 80)
    assert "meta.example" in str(info.value)
    assert "169.254.169.254" in str(info.value)


def test_default_resolver_handles_a_literal_without_dns() -> None:
    # No resolver injected: literals never touch DNS.
    assert DefaultNetworkPolicy([]).resolve("93.184.216.34", 80).address == "93.184.216.34"
