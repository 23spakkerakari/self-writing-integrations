"""Verify and load an offline bundle (spec 4.1, 8.1.1; ADR 0022; plan M1 "Bundle format").

``carto-edge analyze`` writes ``<name>.carto/`` with ``manifest.json``, ``signature.json`` and the
data files of :data:`carto_schema.bundle.DATA_FILES`. Core trusts nothing in it until
:func:`verify_bundle` has passed, in this order: ``signature.json`` parses, the sha256 of the
exact ``manifest.json`` bytes equals ``manifest_sha256``, the Ed25519 signature over those bytes
verifies with the embedded public key (whose id must match ``key_id``), the manifest parses, it
lists only known data files and the events file, the directory holds nothing else, and every
listed file has the recorded size and sha256. Only then does :func:`iter_events` read a single
event, streaming ``events.ndjson.zst`` line by line through zstd and validating every line as a
:class:`~carto_schema.event.CanonicalEvent` (so a raw value cannot arrive in a slot the contract
does not have, spec 2.3 invariant 2); the first bad line stops the load and the error names the
line number, never its content.

:func:`load_bundle` writes chunks of at most :data:`DEFAULT_CHUNK_SIZE` events through the same
writer as ``ingest-api`` with ``batch_id = derive_ulid(created_at_ms, bundle_id, chunk index)``
and the same ledger-then-writer protocol (ADR 0015), so loading a bundle twice writes nothing
the second time and an interrupted load resumes at the first chunk the ledger lacks.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import zstandard
from pydantic import ValidationError

from carto_common.crypto import CryptoError, VerifyKey, b64url_decode
from carto_common.ids import derive_ulid
from carto_common.logging import get_logger
from carto_core.ingest.ledger import LedgerEntry
from carto_schema.bundle import (
    BUNDLE_VERSION,
    DATA_FILES,
    EVENTS_FILE,
    MANIFEST_FILE,
    SIGNATURE_FILE,
    BundleManifest,
    BundleSignature,
)
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import MAX_EVENTS_PER_BATCH

if TYPE_CHECKING:
    from carto_core.db.clickhouse import EventWriter
    from carto_core.ingest.ledger import BatchLedger

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MAX_LINE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_SIGNATURE_BYTES",
    "BundleError",
    "LoadResult",
    "VerifiedBundle",
    "bundle_source_id",
    "iter_events",
    "load_bundle",
    "verify_bundle",
]

MAX_MANIFEST_BYTES: Final = 16 * 1024 * 1024
MAX_SIGNATURE_BYTES: Final = 64 * 1024
MAX_LINE_BYTES: Final = 4 * 1024 * 1024
"""Longest event line accepted: a maximal canonical event is well under 1 MiB."""
DEFAULT_CHUNK_SIZE: Final = MAX_EVENTS_PER_BATCH
_HASH_CHUNK: Final = 1024 * 1024
_READ_CHUNK: Final = 256 * 1024
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

log = get_logger(component="bundle")


class BundleError(Exception):
    """The bundle is not intact or not a bundle. The message never carries event content."""


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    """A bundle whose signature and digests have been checked."""

    manifest: BundleManifest
    signature: BundleSignature
    path: Path


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What :func:`load_bundle` did: events written, chunks seen, chunks already in the ledger."""

    events_written: int
    chunks: int
    duplicates: int


def _read_small(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        msg = f"cannot read {path.name}"
        raise BundleError(msg) from exc
    if len(data) > limit:
        msg = f"{path.name} is larger than {limit} bytes"
        raise BundleError(msg)
    return data


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        msg = f"cannot read {path.name}"
        raise BundleError(msg) from exc
    return digest.hexdigest(), size


def verify_bundle(path: Path) -> VerifiedBundle:
    """Check signature, manifest and every digest; raise :class:`BundleError` otherwise."""
    if not path.is_dir():
        msg = f"{path} is not a bundle directory"
        raise BundleError(msg)
    try:
        signature = BundleSignature.model_validate_json(
            _read_small(path / SIGNATURE_FILE, MAX_SIGNATURE_BYTES)
        )
    except ValidationError as exc:
        msg = f"{SIGNATURE_FILE} is not a bundle signature"
        raise BundleError(msg) from exc
    manifest_bytes = _read_small(path / MANIFEST_FILE, MAX_MANIFEST_BYTES)
    if not hmac.compare_digest(
        hashlib.sha256(manifest_bytes).hexdigest(), signature.manifest_sha256
    ):
        msg = f"{MANIFEST_FILE} does not match the digest in {SIGNATURE_FILE}"
        raise BundleError(msg)
    try:
        key = VerifyKey.from_text(signature.public_key)
    except CryptoError as exc:
        msg = f"{SIGNATURE_FILE} carries an unusable public key"
        raise BundleError(msg) from exc
    if not hmac.compare_digest(key.key_id, signature.key_id):
        msg = f"{SIGNATURE_FILE} key id does not match its public key"
        raise BundleError(msg)
    try:
        key.verify(b64url_decode(signature.signature), manifest_bytes)
    except CryptoError as exc:
        msg = f"{MANIFEST_FILE} signature is invalid"
        raise BundleError(msg) from exc
    try:
        manifest = BundleManifest.model_validate_json(manifest_bytes)
    except ValidationError as exc:
        msg = f"{MANIFEST_FILE} is not a bundle manifest"
        raise BundleError(msg) from exc
    if manifest.bundle_version != BUNDLE_VERSION:
        msg = f"bundle version {manifest.bundle_version!r} is not supported"
        raise BundleError(msg)
    unknown = sorted(set(manifest.files) - set(DATA_FILES))
    if unknown:
        msg = f"manifest lists files that are not bundle data files: {', '.join(unknown)}"
        raise BundleError(msg)
    if EVENTS_FILE not in manifest.files:
        msg = f"manifest does not list {EVENTS_FILE}"
        raise BundleError(msg)
    allowed = {MANIFEST_FILE, SIGNATURE_FILE, *manifest.files}
    for entry in sorted(path.iterdir()):
        if entry.name not in allowed or not entry.is_file():
            msg = f"unexpected entry in bundle: {entry.name}"
            raise BundleError(msg)
    for name, recorded in manifest.files.items():
        file_path = path / name
        if not file_path.is_file():
            msg = f"listed file {name} is missing"
            raise BundleError(msg)
        digest, size = _sha256_and_size(file_path)
        if size != recorded.bytes:
            msg = f"{name} has {size} bytes, manifest records {recorded.bytes}"
            raise BundleError(msg)
        if not hmac.compare_digest(digest, recorded.sha256):
            msg = f"{name} digest does not match the manifest"
            raise BundleError(msg)
    return VerifiedBundle(manifest=manifest, signature=signature, path=path)


def _lines(reader: zstandard.ZstdDecompressionReader) -> Iterator[bytes]:
    pending = bytearray()
    while True:
        chunk = reader.read(_READ_CHUNK)
        if not chunk:
            break
        pending += chunk
        while (newline := pending.find(b"\n")) >= 0:
            yield bytes(pending[:newline])
            del pending[: newline + 1]
        if len(pending) > MAX_LINE_BYTES:
            msg = f"{EVENTS_FILE} holds a line longer than {MAX_LINE_BYTES} bytes"
            raise BundleError(msg)
    if pending:
        yield bytes(pending)


def iter_events(verified: VerifiedBundle) -> Iterator[CanonicalEvent]:
    """Stream the events file, validating every line; fail fast on the first bad one."""
    manifest = verified.manifest
    count = 0
    try:
        with (
            (verified.path / EVENTS_FILE).open("rb") as raw,
            zstandard.ZstdDecompressor().stream_reader(raw) as reader,
        ):
            for number, line in enumerate(_lines(reader), start=1):
                text = line.rstrip(b"\r")
                if not text:
                    continue
                try:
                    event = CanonicalEvent.model_validate_json(text)
                except ValidationError:
                    msg = f"{EVENTS_FILE} line {number} is not a canonical event"
                    raise BundleError(msg) from None
                if event.tenant_id != manifest.tenant_id:
                    msg = f"{EVENTS_FILE} line {number} belongs to another tenant"
                    raise BundleError(msg)
                count += 1
                yield event
    except zstandard.ZstdError as exc:
        msg = f"{EVENTS_FILE} is not a valid zstd stream"
        raise BundleError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {EVENTS_FILE}"
        raise BundleError(msg) from exc
    if count != manifest.counts.events:
        msg = f"manifest counts {manifest.counts.events} events, {EVENTS_FILE} holds {count}"
        raise BundleError(msg)


def bundle_source_id(manifest: BundleManifest) -> str:
    """The ledger ``source_id`` of a bundle's chunks: ``bundle_<bundle id, lower case>``."""
    return f"bundle_{manifest.bundle_id.lower()}"


def load_bundle(
    verified: VerifiedBundle,
    writer: EventWriter,
    ledger: BatchLedger,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    clock: Callable[[], datetime] | None = None,
) -> LoadResult:
    """Write the events in chunks through the ledger-then-writer protocol (ADR 0015)."""
    if not 1 <= chunk_size <= MAX_EVENTS_PER_BATCH:
        msg = f"chunk_size must be between 1 and {MAX_EVENTS_PER_BATCH}"
        raise ValueError(msg)
    manifest = verified.manifest
    now = clock if clock is not None else lambda: datetime.now(UTC)
    created_at_ms = (manifest.created_at - _EPOCH) // timedelta(milliseconds=1)
    source_id = bundle_source_id(manifest)
    written = 0
    duplicates = 0
    index = 0

    def flush(chunk: list[CanonicalEvent]) -> None:
        nonlocal written, duplicates, index
        batch_id = derive_ulid(created_at_ms, manifest.bundle_id, str(index))
        if ledger.contains(manifest.tenant_id, batch_id):
            duplicates += 1
            log.info("bundle chunk already loaded", bundle_id=manifest.bundle_id, chunk=index)
        else:
            result = writer.write_events(chunk)
            ledger.record(LedgerEntry(manifest.tenant_id, batch_id, source_id, len(chunk), now()))
            written += result.events
            log.info(
                "bundle chunk written",
                bundle_id=manifest.bundle_id,
                chunk=index,
                events=result.events,
                identifiers=result.identifiers,
            )
        index += 1

    chunk: list[CanonicalEvent] = []
    for event in iter_events(verified):
        chunk.append(event)
        if len(chunk) >= chunk_size:
            flush(chunk)
            chunk = []
    if chunk:
        flush(chunk)
    return LoadResult(events_written=written, chunks=index, duplicates=duplicates)
