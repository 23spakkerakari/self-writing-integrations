"""carto_common.ids: ULID format, ordering, derivation and round trips (spec 5.4, 7.1, 8.1)."""

from __future__ import annotations

import hashlib
import random
import secrets
import time

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from carto_common.ids import (
    CROCKFORD_ALPHABET,
    MAX_TIMESTAMP_MS,
    RANDOMNESS_BYTES,
    ULID_CHARS,
    ULID_LENGTH,
    ULID_PATTERN,
    ULID_REGEX,
    derive_ulid,
    is_ulid,
    new_ulid,
    ulid_from_parts,
    ulid_timestamp_ms,
)

FORBIDDEN_LETTERS = set("ILOU")
TS = 1_758_585_600_000  # 2026-09-23T00:00:00Z, the first simulator day (M0 plan)

timestamps = st.integers(min_value=0, max_value=MAX_TIMESTAMP_MS)
randomness = st.binary(min_size=RANDOMNESS_BYTES, max_size=RANDOMNESS_BYTES)


# ---------------------------------------------------------------------------------------------
# Format and alphabet
# ---------------------------------------------------------------------------------------------


def test_new_ulid_has_the_canonical_shape() -> None:
    ulid = new_ulid()
    assert len(ulid) == ULID_LENGTH == 26
    assert ULID_PATTERN.fullmatch(ulid)
    assert ulid[0] in "01234567"
    assert is_ulid(ulid)


def test_alphabet_is_crockford_base32_without_ilou() -> None:
    assert len(CROCKFORD_ALPHABET) == 32
    assert len(set(CROCKFORD_ALPHABET)) == 32
    assert not FORBIDDEN_LETTERS & set(CROCKFORD_ALPHABET)
    assert ULID_REGEX == r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
    assert ULID_CHARS == r"[0-7][0-9A-HJKMNP-TV-Z]{25}"  # the log redactor embeds this one
    for _ in range(200):
        assert not FORBIDDEN_LETTERS & set(new_ulid())


def test_new_ulid_carries_the_current_millisecond() -> None:
    before = time.time_ns() // 1_000_000
    ulid = new_ulid()
    after = time.time_ns() // 1_000_000
    assert before <= ulid_timestamp_ms(ulid) <= after


def test_new_ulids_are_unique() -> None:
    assert len({new_ulid() for _ in range(1000)}) == 1000


def test_known_vectors() -> None:
    # Timestamp 1469918176385 renders as 01ARYZ6S41 (the ULID specification's own example).
    assert ulid_from_parts(1469918176385, bytes(10)) == "01ARYZ6S41" + "0" * 16
    assert ulid_from_parts(0, bytes(10)) == "0" * 26
    assert ulid_from_parts(MAX_TIMESTAMP_MS, b"\xff" * 10) == "7" + "Z" * 25


# ---------------------------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------------------------


def test_lexical_order_follows_timestamp_order() -> None:
    stamps = sorted(random.sample(range(MAX_TIMESTAMP_MS + 1), 500))
    ulids = [ulid_from_parts(stamp, secrets.token_bytes(RANDOMNESS_BYTES)) for stamp in stamps]
    assert ulids == sorted(ulids)
    assert [ulid_timestamp_ms(ulid) for ulid in ulids] == stamps


@given(first=timestamps, second=timestamps, left=randomness, right=randomness)
def test_order_is_decided_by_the_timestamp(
    first: int, second: int, left: bytes, right: bytes
) -> None:
    assume(first != second)
    assert (ulid_from_parts(first, left) < ulid_from_parts(second, right)) == (first < second)


# ---------------------------------------------------------------------------------------------
# ulid_from_parts validation and round trip
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("timestamp_ms", [-1, MAX_TIMESTAMP_MS + 1, 2**64])
def test_ulid_from_parts_rejects_timestamps_out_of_range(timestamp_ms: int) -> None:
    with pytest.raises(ValueError, match="timestamp_ms"):
        ulid_from_parts(timestamp_ms, bytes(10))


@pytest.mark.parametrize("size", [0, 9, 11, 16])
def test_ulid_from_parts_rejects_randomness_of_the_wrong_size(size: int) -> None:
    with pytest.raises(ValueError, match="exactly 10 bytes"):
        ulid_from_parts(TS, bytes(size))


def test_ulid_from_parts_accepts_the_range_boundaries() -> None:
    assert is_ulid(ulid_from_parts(0, bytes(10)))
    assert is_ulid(ulid_from_parts(MAX_TIMESTAMP_MS, bytes(10)))


@given(timestamp_ms=timestamps, randomness=randomness)
def test_ulid_from_parts_round_trips_the_timestamp(timestamp_ms: int, randomness: bytes) -> None:
    ulid = ulid_from_parts(timestamp_ms, randomness)
    assert len(ulid) == 26
    assert ULID_PATTERN.fullmatch(ulid)
    assert is_ulid(ulid)
    assert ulid_timestamp_ms(ulid) == timestamp_ms
    assert ulid_from_parts(timestamp_ms, randomness) == ulid


# ---------------------------------------------------------------------------------------------
# derive_ulid (spec 8.1: event_id derived deterministically from source position)
# ---------------------------------------------------------------------------------------------


def test_derive_ulid_is_deterministic() -> None:
    first = derive_ulid(TS, "src_orders_log", "orders-2026-09-23.log:line:17")
    second = derive_ulid(TS, "src_orders_log", "orders-2026-09-23.log:line:17")
    assert first == second
    assert is_ulid(first)


def test_derive_ulid_differs_per_part_and_per_timestamp() -> None:
    base = derive_ulid(TS, "src_orders_log", "orders.log:line:17")
    assert derive_ulid(TS, "src_orders_log", "orders.log:line:18") != base
    assert derive_ulid(TS, "src_webstore_log", "orders.log:line:17") != base
    assert derive_ulid(TS + 1, "src_orders_log", "orders.log:line:17") != base
    assert derive_ulid(TS, "src_orders_log") != base
    # The 0x00 separator keeps part boundaries: ("ab", "c") is not ("a", "bc").
    assert derive_ulid(TS, "ab", "c") != derive_ulid(TS, "a", "bc")


def test_derive_ulid_round_trips_the_timestamp() -> None:
    assert ulid_timestamp_ms(derive_ulid(TS, "src", "locator")) == TS
    assert ulid_timestamp_ms(derive_ulid(0, "src")) == 0
    assert ulid_timestamp_ms(derive_ulid(MAX_TIMESTAMP_MS, "src")) == MAX_TIMESTAMP_MS


def test_derive_ulid_treats_bytes_and_utf8_text_alike() -> None:
    assert derive_ulid(TS, b"src", "loc") == derive_ulid(TS, "src", b"loc")
    assert derive_ulid(TS, "café") == derive_ulid(TS, "café".encode())


def test_derive_ulid_follows_the_documented_recipe() -> None:
    digest = hashlib.sha256(b"src_orders_log\x00orders.log:line:17").digest()
    expected = ulid_from_parts(TS, digest[:RANDOMNESS_BYTES])
    assert derive_ulid(TS, "src_orders_log", "orders.log:line:17") == expected


def test_derive_ulid_needs_at_least_one_part() -> None:
    with pytest.raises(ValueError, match="at least one part"):
        derive_ulid(TS)


def test_derive_ulid_validates_the_timestamp() -> None:
    with pytest.raises(ValueError, match="timestamp_ms"):
        derive_ulid(MAX_TIMESTAMP_MS + 1, "src")


# ---------------------------------------------------------------------------------------------
# is_ulid and ulid_timestamp_ms rejections
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "01ARYZ6S41TSV4RRFFQ69G5FA",  # 25 characters
        "01ARYZ6S41TSV4RRFFQ69G5FAVX",  # 27 characters
        "01aryz6s41tsv4rrffq69g5fav",  # lower case is not canonical
        "81ARYZ6S41TSV4RRFFQ69G5FAV",  # first character above 7 overflows 128 bits
        "01ARYZ6S41TSV4RRFFQ69G5FAI",  # I is not in the alphabet
        "01ARYZ6S41TSV4RRFFQ69G5FAL",
        "01ARYZ6S41TSV4RRFFQ69G5FAO",
        "01ARYZ6S41TSV4RRFFQ69G5FAU",
        "01ARYZ6S41TSV4RRFFQ69G5FA-",
        "01ARYZ6S41TSV4RRFFQ69G5FAV\n",
        " 01ARYZ6S41TSV4RRFFQ69G5FAV",
    ],
)
def test_non_ulids_are_rejected(value: str) -> None:
    assert not is_ulid(value)
    with pytest.raises(ValueError, match="not a canonical"):
        ulid_timestamp_ms(value)


def test_is_ulid_accepts_the_specification_example() -> None:
    assert is_ulid("01ARYZ6S41TSV4RRFFQ69G5FAV")
    assert ulid_timestamp_ms("01ARYZ6S41TSV4RRFFQ69G5FAV") == 1469918176385
