"""carto_edge.audit: the hash-chained NDJSON edge audit file (spec 14.9, ADR 0014)."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from carto_common.logging import EMAIL_MASK, TOKEN_MASK
from carto_edge.audit import (
    GENESIS_HASH,
    AuditChainError,
    AuditRow,
    EdgeAudit,
    VerifyResult,
    canonical_json,
    verify,
)


def rows_of(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_record_appends_chained_rows(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    first = audit.record("reveal", "user:42", "vault", {"tokens": 3, "revealed": 2}, "req-1")
    second = audit.record("tokenize", "user:42", "query", {"forms": 4})
    audit.close()

    assert isinstance(first, AuditRow)
    assert first.seq == 1 and second.seq == 2
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.row_hash
    assert first.request_id == "req-1" and second.request_id == ""
    assert first.ts.utcoffset() == timedelta(0)

    rows = rows_of(path)
    assert len(rows) == 2
    assert rows[0]["action"] == "reveal"
    assert rows[0]["actor"] == "user:42"
    assert rows[0]["target"] == "vault"
    assert rows[0]["details"] == {"tokens": 3, "revealed": 2}
    without_hash = {key: value for key, value in rows[0].items() if key != "row_hash"}
    expected = hashlib.sha256(
        (GENESIS_HASH + canonical_json(without_hash)).encode("utf-8")
    ).hexdigest()
    assert rows[0]["row_hash"] == expected == first.row_hash


def test_verify_accepts_an_intact_chain_and_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    assert verify(path) == VerifyResult(ok=True, rows=0, first_bad_line=None)
    audit = EdgeAudit(path)
    for n in range(5):
        audit.record("config.change", "admin", f"setting-{n}", {"n": n})
    audit.close()
    assert verify(path) == VerifyResult(ok=True, rows=5, first_bad_line=None)
    assert EdgeAudit.verify(path).ok


def test_reopen_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    last = audit.record("a", "x", "y", {})
    audit.close()
    audit = EdgeAudit(path)
    assert audit.last_hash == last.row_hash
    assert audit.seq == 1
    row = audit.record("b", "x", "y", {})
    audit.close()
    assert row.seq == 2 and row.prev_hash == last.row_hash
    assert verify(path).ok
    assert verify(path).rows == 2


def test_tampering_with_a_row_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    for n in range(3):
        audit.record("reveal", "user:1", "vault", {"revealed": n})
    audit.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["details"]["revealed"] = 99
    lines[1] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(path) == VerifyResult(ok=False, rows=3, first_bad_line=2)
    with pytest.raises(AuditChainError, match="line 2"):
        EdgeAudit(path)


def test_deleting_a_row_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    for n in range(3):
        audit.record("reveal", "user:1", "vault", {"revealed": n})
    audit.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify(path)
    assert not result.ok and result.first_bad_line == 2


def test_truncating_the_tail_is_not_detectable_but_the_chain_still_verifies(
    tmp_path: Path,
) -> None:
    # The daily anchor export (spec 14.9) is what catches a truncated tail; the chain itself
    # only proves what remains is in order.
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    for n in range(3):
        audit.record("reveal", "user:1", "vault", {"revealed": n})
    audit.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    assert verify(path) == VerifyResult(ok=True, rows=2, first_bad_line=None)


def test_garbage_lines_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    audit.record("a", "x", "y", {})
    audit.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert verify(path) == VerifyResult(ok=False, rows=2, first_bad_line=2)
    with pytest.raises(AuditChainError):
        EdgeAudit(path)


def test_details_are_masked_defensively(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    row = audit.record(
        "reveal",
        "user:1",
        "vault",
        {
            "purpose": "ticket for jane@example.com about t1.q8Jm0h3cR2VfZp4Lx9sT1w",
            "nested": {"values": ["SO-0004471", 7, None, True]},
            "count": 2,
        },
    )
    audit.close()
    text = path.read_text(encoding="utf-8")
    assert "jane@example.com" not in text
    assert "t1.q8Jm0h3cR2VfZp4Lx9sT1w" not in text
    assert "SO-0004471" not in text
    purpose = row.details["purpose"]
    assert isinstance(purpose, str)
    assert EMAIL_MASK in purpose and TOKEN_MASK in purpose
    nested = row.details["nested"]
    assert isinstance(nested, dict)
    assert nested["values"][1:] == [7, None, True]
    assert row.details["count"] == 2
    assert verify(path).ok


def test_non_json_detail_values_are_stringified(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    row = audit.record("a", "x", "y", {"when": datetime(2026, 10, 8, tzinfo=UTC), "p": Path("x")})
    audit.close()
    assert row.details["when"] == "2026-10-08T00:00:00+00:00"
    assert row.details["p"] == "x"
    assert verify(path).ok


def test_concurrent_writers_keep_the_chain_intact(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)

    def writer(name: str) -> None:
        for n in range(40):
            audit.record("reveal", name, "vault", {"n": n})

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    audit.close()
    result = verify(path)
    assert result == VerifyResult(ok=True, rows=200, first_bad_line=None)
    assert [row["seq"] for row in rows_of(path)] == list(range(1, 201))


def test_long_fields_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    audit = EdgeAudit(path)
    row = audit.record("a" * 500, "b" * 500, "c" * 5000, {"k" * 500: "v" * 20000})
    audit.close()
    assert len(row.action) <= 128
    assert len(row.actor) <= 256
    assert len(row.target) <= 1024
    assert all(len(key) <= 128 for key in row.details)
    assert all(len(str(value)) <= 4096 for value in row.details.values())
    assert verify(path).ok


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": {"d": [1, 2], "c": "\u00e9"}}) == (
        '{"a":{"c":"\\u00e9","d":[1,2]},"b":1}'
    )


def test_two_writers_on_one_file_keep_one_chain(tmp_path: Path) -> None:
    """The gateway and the operator CLI append to the same file (review finding)."""
    path = tmp_path / "audit.ndjson"
    gateway = EdgeAudit(path)
    gateway.record("reveal", "user:a", "vault", {"n": 1})
    cli = EdgeAudit(path)  # opened while the gateway keeps its handle
    cli.record("key.rotate", "cli:ops", "tenant-key", {"active_version": 2})
    gateway.record("reveal", "user:a", "vault", {"n": 2})  # must chain to the CLI row
    cli.record("secret.set", "cli:ops", "local://x", {"bytes": 20})
    gateway.close()
    cli.close()
    assert verify(path) == VerifyResult(ok=True, rows=4, first_bad_line=None)
    assert [row["seq"] for row in rows_of(path)] == [1, 2, 3, 4]
    EdgeAudit(path).close()  # and the next start opens it
