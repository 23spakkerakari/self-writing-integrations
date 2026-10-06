"""ULIDs without a third-party dependency.

Spec 5.4 step 2 and 7.1: the edge assigns each canonical event an ``event_id`` that is a ULID.
Spec 8.1 ("Common requirements"): the id is derived deterministically from the source position
so that core can dedupe at-least-once delivery by ``event_id``. ADR 0006 keys ground truth by
``<source_id>:<locator>``; M1 derives ``event_id = derive_ulid(observed_at_ms, source_id,
locator)`` and writes the locator map the eval harness joins on.

A ULID is 128 bits: a 48-bit millisecond timestamp followed by 80 bits of randomness (hash
output for derived ids), rendered as 26 Crockford base32 characters (``0-9A-HJKMNP-TV-Z``, no
``I``, ``L``, ``O`` or ``U``). The first character carries the two spare bits plus the top three
timestamp bits, so it is always ``0`` to ``7``. Lexical order of the string follows timestamp
order, which is what ClickHouse ordering keys and log readers want. Only the canonical upper-case
rendering is accepted on input.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Final

__all__ = [
    "CROCKFORD_ALPHABET",
    "MAX_TIMESTAMP_MS",
    "RANDOMNESS_BYTES",
    "ULID_CHARS",
    "ULID_LENGTH",
    "ULID_PATTERN",
    "ULID_REGEX",
    "derive_ulid",
    "is_ulid",
    "new_ulid",
    "ulid_from_parts",
    "ulid_timestamp_ms",
]

CROCKFORD_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
"""Crockford base32 alphabet in value order (index is the 5-bit value)."""

ULID_LENGTH: Final = 26
ULID_CHARS: Final = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
"""Unanchored form of :data:`ULID_REGEX`, for embedding in larger patterns (the log redactor)."""
ULID_REGEX: Final = rf"^{ULID_CHARS}$"
"""Source pattern for ``pydantic.Field(pattern=...)`` and JSON Schema ``pattern``."""
ULID_PATTERN: Final = re.compile(ULID_REGEX)

TIMESTAMP_BITS: Final = 48
RANDOMNESS_BITS: Final = 80
RANDOMNESS_BYTES: Final = RANDOMNESS_BITS // 8
MAX_TIMESTAMP_MS: Final = (1 << TIMESTAMP_BITS) - 1
"""Largest encodable timestamp, 2**48 - 1 milliseconds (the year 10889)."""

_TIMESTAMP_CHARS: Final = 10
_DECODE: Final = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}
_PART_SEPARATOR: Final = b"\x00"


def _encode(value: int) -> str:
    """Render a 128-bit integer as 26 Crockford base32 characters, most significant first."""
    return "".join(CROCKFORD_ALPHABET[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def ulid_from_parts(timestamp_ms: int, randomness: bytes) -> str:
    """Build a ULID from a millisecond timestamp and exactly 10 bytes of randomness.

    Raises ``ValueError`` when the timestamp is outside ``0..2**48-1`` or the randomness is not
    exactly 10 bytes long.
    """
    if not 0 <= timestamp_ms <= MAX_TIMESTAMP_MS:
        msg = f"timestamp_ms must be between 0 and {MAX_TIMESTAMP_MS}, got {timestamp_ms}"
        raise ValueError(msg)
    if len(randomness) != RANDOMNESS_BYTES:
        msg = f"randomness must be exactly {RANDOMNESS_BYTES} bytes, got {len(randomness)}"
        raise ValueError(msg)
    return _encode((timestamp_ms << RANDOMNESS_BITS) | int.from_bytes(randomness, "big"))


def new_ulid() -> str:
    """Return a fresh random ULID for the current time (spec 5.4 step 2)."""
    return ulid_from_parts(time.time_ns() // 1_000_000, secrets.token_bytes(RANDOMNESS_BYTES))


def derive_ulid(timestamp_ms: int, *parts: str | bytes) -> str:
    """Return the ULID that ``timestamp_ms`` and ``parts`` always map to (spec 8.1).

    The 80 non-timestamp bits are the first 10 bytes of SHA-256 over the parts, each ``str``
    UTF-8 encoded, joined with a single ``0x00`` separator. M1 derives ``event_id`` as
    ``derive_ulid(observed_at_ms, source_id, locator)``, so a record read twice (at-least-once
    delivery, spec 8.1) gets the same id and core can dedupe on it. At least one part is
    required: with none, every event in the same millisecond would collide.
    """
    if not parts:
        msg = "derive_ulid needs at least one part to hash"
        raise ValueError(msg)
    material = _PART_SEPARATOR.join(
        part.encode("utf-8") if isinstance(part, str) else part for part in parts
    )
    digest = hashlib.sha256(material).digest()
    return ulid_from_parts(timestamp_ms, digest[:RANDOMNESS_BYTES])


def ulid_timestamp_ms(ulid: str) -> int:
    """Return the millisecond timestamp encoded in the first 10 characters of a ULID."""
    if not is_ulid(ulid):
        msg = "value is not a canonical 26-character ULID"
        raise ValueError(msg)
    value = 0
    for char in ulid[:_TIMESTAMP_CHARS]:
        value = (value << 5) | _DECODE[char]
    return value


def is_ulid(value: str) -> bool:
    """True when ``value`` is a canonical (upper-case) 26-character ULID."""
    return ULID_PATTERN.fullmatch(value) is not None
