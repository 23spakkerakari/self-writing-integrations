"""``carto-ctl pki init``: the private CA and per-service certificates for Compose (spec 14.4).

Writes ``ca.crt`` and ``<service>.crt``/``<service>.key`` for every service into ``--out``
(default ``./pki``) and ``ca.key`` into ``--ca-key-dir`` (default: ``--out``). Keeping the CA key
in a directory no running service mounts means a compromised service cannot mint certificates
(spec 14.4, 15); the Compose stack puts it on its own volume that only ``pki-init`` sees. Each
leaf allows server and client authentication, so the same pair serves a listener and
authenticates it as a client of another service, and carries SANs for the service name,
``localhost`` and any ``--dns``/``--ip`` given. Keys are written with owner-only permissions and
never overwritten: a directory holding any of the keys this run would write is refused before
anything is touched. With ``--if-missing`` a complete earlier run (every file this run would
write is present) is a success that writes nothing, which is what a Compose one-shot job needs
on its second start; a partial one is still refused. The output lists certificate fingerprints
only. Kubernetes installs use cert-manager instead (M6).
"""

from __future__ import annotations

import argparse
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from carto_common.pki import (
    LEAF_VALID_DAYS,
    PkiError,
    certificate_fingerprint,
    create_ca,
    issue_certificate,
    write_pem,
)
from carto_ctl.exit_codes import EXIT_FAILURE, EXIT_OK

if TYPE_CHECKING:
    import datetime as dt

    from carto_ctl.registry import Invocation

__all__ = [
    "DEFAULT_OUT",
    "DEFAULT_SERVICES",
    "MAX_DAYS",
    "PkiInitResult",
    "configure_pki_init",
    "initialize_pki",
    "pki_complete",
    "pki_init",
]

DEFAULT_SERVICES: Final[tuple[str, ...]] = ("edge-gateway", "ingest-api", "otel-collector", "api")
DEFAULT_OUT: Final = Path("pki")
MAX_DAYS: Final = 3650
_SERVICE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DNS_PATTERN: Final = re.compile(
    r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$"
)


@dataclass(frozen=True, slots=True)
class PkiInitResult:
    out: Path
    ca_fingerprint: str
    services: tuple[tuple[str, str], ...]
    """``(service, certificate fingerprint)`` in the order written."""
    san_names: tuple[str, ...]
    san_addresses: tuple[str, ...]
    days: int


def _service_list(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for raw in text.split(","):
        name = raw.strip()
        if not _SERVICE_PATTERN.match(name):
            msg = f"service name must match {_SERVICE_PATTERN.pattern}"
            raise argparse.ArgumentTypeError(msg)
        if name not in names:
            names.append(name)
    return tuple(names)


def _dns_name(text: str) -> str:
    if len(text) > 253 or not _DNS_PATTERN.match(text):
        msg = "not a DNS name"
        raise argparse.ArgumentTypeError(msg)
    return text


def _ip_address(text: str) -> str:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        msg = "not an IP address"
        raise argparse.ArgumentTypeError(msg) from None


def _days(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        msg = "must be a whole number of days"
        raise argparse.ArgumentTypeError(msg) from None
    if not 1 <= value <= MAX_DAYS:
        msg = f"must be between 1 and {MAX_DAYS}"
        raise argparse.ArgumentTypeError(msg)
    return value


def configure_pki_init(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Directory to write into (default {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--ca-key-dir",
        type=Path,
        default=None,
        help="Directory for ca.key, kept away from the services (default: --out)",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Succeed without writing when a complete earlier run is present (Compose jobs)",
    )
    parser.add_argument(
        "--services",
        type=_service_list,
        default=DEFAULT_SERVICES,
        metavar="NAME[,NAME...]",
        help=f"Services to issue for (default {','.join(DEFAULT_SERVICES)})",
    )
    parser.add_argument(
        "--dns",
        action="append",
        type=_dns_name,
        default=[],
        metavar="NAME",
        help="Extra DNS SAN on every service certificate (repeatable)",
    )
    parser.add_argument(
        "--ip",
        action="append",
        type=_ip_address,
        default=[],
        metavar="ADDR",
        help="Extra IP SAN on every service certificate (repeatable)",
    )
    parser.add_argument(
        "--days",
        type=_days,
        default=LEAF_VALID_DAYS,
        help=f"Service certificate lifetime in days (default {LEAF_VALID_DAYS}, spec 14.4)",
    )


def _planned_files(out: Path, ca_key_dir: Path, services: tuple[str, ...]) -> list[Path]:
    files = [out / "ca.crt", ca_key_dir / "ca.key"]
    for service in services:
        files.extend((out / f"{service}.crt", out / f"{service}.key"))
    return files


def pki_complete(out: Path, services: tuple[str, ...], ca_key_dir: Path | None = None) -> bool:
    """True when every file :func:`initialize_pki` would write for ``services`` exists."""
    planned = _planned_files(out, ca_key_dir if ca_key_dir is not None else out, services)
    return all(path.is_file() for path in planned)


def initialize_pki(
    out: Path,
    services: tuple[str, ...],
    dns_names: tuple[str, ...],
    ip_addresses: tuple[str, ...],
    days: int,
    *,
    ca_key_dir: Path | None = None,
    now: dt.datetime | None = None,
) -> PkiInitResult:
    """Create the CA and one server+client pair per service; refuse to overwrite any key."""
    if not services:
        msg = "at least one service is required"
        raise PkiError(msg)
    key_dir = ca_key_dir if ca_key_dir is not None else out
    key_paths = [key_dir / "ca.key", *(out / f"{service}.key" for service in services)]
    existing = [str(path) for path in key_paths if path.exists()]
    if existing:
        msg = f"refusing to overwrite existing private key(s): {', '.join(existing)}"
        raise PkiError(msg)
    ca = create_ca(now=now)
    key_dir.mkdir(parents=True, exist_ok=True)
    write_pem(ca, out / "ca.crt", key_dir / "ca.key")
    issued: list[tuple[str, str]] = []
    for service in services:
        names: list[str] = []
        for name in (service, "localhost", *dns_names):
            if name not in names:
                names.append(name)
        leaf = issue_certificate(
            ca,
            service,
            dns_names=tuple(names),
            ip_addresses=ip_addresses,
            valid_days=days,
            now=now,
        )
        write_pem(leaf, out / f"{service}.crt", out / f"{service}.key")
        issued.append((service, certificate_fingerprint(leaf.certificate)))
    return PkiInitResult(
        out=out,
        ca_fingerprint=certificate_fingerprint(ca.certificate),
        services=tuple(issued),
        san_names=("<service>", "localhost", *dns_names),
        san_addresses=tuple(ip_addresses),
        days=days,
    )


def pki_init(invocation: Invocation) -> int:
    args = invocation.args
    services = tuple(args.services)
    if args.if_missing and pki_complete(args.out, services, args.ca_key_dir):
        invocation.out.write(
            f"PKI already present in {args.out} for {', '.join(services)}; nothing written.\n"
        )
        return EXIT_OK
    try:
        result = initialize_pki(
            args.out,
            services,
            tuple(args.dns),
            tuple(args.ip),
            args.days,
            ca_key_dir=args.ca_key_dir,
        )
    except (PkiError, OSError) as exc:
        invocation.err.write(f"carto-ctl pki init: {exc}\n")
        return EXIT_FAILURE
    out = invocation.out
    out.write(f"ca.crt  {result.ca_fingerprint}\n")
    for service, fingerprint in result.services:
        out.write(f"{service}.crt  {fingerprint}\n")
    sans = ", ".join((*result.san_names, *result.san_addresses))
    out.write(
        f"wrote {len(result.services)} service certificate pairs valid {result.days} days "
        f"(SANs: {sans}) and the CA to {result.out}; keep every *.key private.\n"
    )
    return EXIT_OK
