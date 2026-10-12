"""carto_edge.state: the cursor store over cursors.sqlite (spec 8.1 "Cursor checkpointing",
ADR 0014): WAL mode, durable across a reopen, JSON objects only, bounded, corrupt rows read as
None with a warning that names the source and never the stored text."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.state import MAX_CURSOR_BYTES, CursorError, CursorStore

NOW = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)


def test_set_get_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursors.sqlite"
    with CursorStore(path, clock=lambda: NOW) as store:
        assert store.get("src_web") is None
        store.set("src_web", {"file": "/data/app.ndjson", "line": 3})
        store.set("src_wms", {"watermark": "2026-10-10T08:00:00Z", "pk": [1, 2]})
        assert store.get("src_web") == {"file": "/data/app.ndjson", "line": 3}
        store.set("src_web", {"file": "/data/app.ndjson", "line": 9})
    with CursorStore(path) as reopened:  # as after a crash and restart
        assert reopened.get("src_web") == {"file": "/data/app.ndjson", "line": 9}
        assert reopened.all() == {
            "src_web": {"file": "/data/app.ndjson", "line": 9},
            "src_wms": {"watermark": "2026-10-10T08:00:00Z", "pk": [1, 2]},
        }
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_cursor_over_the_cap_is_refused(tmp_path: Path) -> None:
    with CursorStore(tmp_path / "c.sqlite") as store:
        store.set("src_web", {"line": 1})
        with pytest.raises(CursorError, match="exceeds") as caught:
            store.set("src_web", {"blob": "x" * MAX_CURSOR_BYTES})
        assert "xxxx" not in str(caught.value)
        assert store.get("src_web") == {"line": 1}  # the previous cursor stays


def test_cursor_must_be_a_json_object(tmp_path: Path) -> None:
    with CursorStore(tmp_path / "c.sqlite") as store:
        with pytest.raises(CursorError):
            store.set("src_web", {"when": datetime.now(UTC)})
        with pytest.raises(CursorError):
            store.set("src_web", {"ratio": float("nan")})
        with pytest.raises(CursorError):
            store.set("src_web", ["not", "a", "mapping"])  # type: ignore[arg-type]
        with pytest.raises(CursorError):
            store.set("", {"line": 1})
        assert store.get("src_web") is None


@pytest.mark.parametrize(
    "stored",
    [
        "{not json",
        "[1, 2, 3]",
        '"a string"',
        '{"value": NaN}',
        "x" * (MAX_CURSOR_BYTES + 1),
        "[" * 30_000 + "]" * 30_000,
    ],
    ids=["broken", "array", "string", "nan", "oversized", "deep"],
)
def test_corrupt_row_reads_as_none_with_a_warning(tmp_path: Path, stored: str) -> None:
    path = tmp_path / "c.sqlite"
    with CursorStore(path) as store:
        store.set("src_web", {"line": 1})
        store.set("src_ok", {"line": 2})
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("UPDATE cursors SET cursor = ? WHERE source_id = ?", (stored, "src_web"))
    finally:
        conn.close()
    with CursorStore(path) as store, structlog.testing.capture_logs() as logs:
        assert store.get("src_web") is None
        assert store.all() == {"src_ok": {"line": 2}}
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warnings
    assert all(entry.get("source_id") == "src_web" for entry in warnings)
    assert stored[:20] not in repr(logs)


def test_concurrent_writers_keep_one_row_per_source(tmp_path: Path) -> None:
    with CursorStore(tmp_path / "c.sqlite") as store:

        def write(worker: int) -> None:
            for line in range(50):
                store.set(f"src_{worker}", {"line": line})

        threads = [threading.Thread(target=write, args=(worker,)) for worker in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert store.all() == {f"src_{worker}": {"line": 49} for worker in range(4)}


def test_repr_carries_no_cursor(tmp_path: Path) -> None:
    with CursorStore(tmp_path / "c.sqlite") as store:
        store.set("src_web", {"file": "secret-name.log", "line": 1})
        assert "secret-name" not in repr(store)


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=40, deadline=None)
@given(cursor=st.dictionaries(st.text(max_size=12), json_values, max_size=6))
def test_round_trip_property(
    tmp_path_factory: pytest.TempPathFactory, cursor: dict[str, Any]
) -> None:
    path = tmp_path_factory.mktemp("cursor") / "c.sqlite"
    with CursorStore(path) as store:
        store.set("src_web", cursor)
        assert store.get("src_web") == cursor
