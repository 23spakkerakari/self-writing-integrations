"""Unstructured text lines: leading timestamp, level token, message (spec 8.2 item 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.parse.text import parse_text

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)
NEW_YORK = ZoneInfo("America/New_York")


def test_parse_text_export_log_line_with_naive_local_timestamp() -> None:
    line = "2026-09-23 21:12:41 INFO PO export finished: 412 POs written to SHIP_20260923_2112.csv"
    parsed = parse_text(line, NEW_YORK)
    assert parsed is not None
    assert parsed.timestamp == datetime(2026, 9, 24, 1, 12, 41, tzinfo=UTC)
    assert parsed.level == "INFO"
    assert parsed.message == "PO export finished: 412 POs written to SHIP_20260923_2112.csv"


def test_parse_text_error_line() -> None:
    line = (
        "2026-10-01 21:14:30 ERROR SFTP upload failed: Permission denied "
        "(/outbound/shipping/SHIP_20261001_2113.csv)"
    )
    parsed = parse_text(line, NEW_YORK)
    assert parsed is not None
    assert parsed.level == "ERROR"
    assert parsed.message.startswith("SFTP upload failed: Permission denied")


def test_parse_text_level_token_variants() -> None:
    for line, level in (
        ("2026-09-23T21:12:41Z [WARN] disk almost full", "WARN"),
        ("2026-09-23T21:12:41Z warning: disk almost full", "warning"),
        ("[2026-09-23 21:12:41] DEBUG tick", "DEBUG"),
        ("2026-09-23T21:12:41Z - INFO - tick", "INFO"),
        ("INFO tick without timestamp", "INFO"),
    ):
        parsed = parse_text(line)
        assert parsed is not None, line
        assert parsed.level == level, line
        assert parsed.message in {"disk almost full", "tick", "tick without timestamp"}, line


def test_parse_text_without_timestamp_or_level() -> None:
    parsed = parse_text("just a message with 42 things")
    assert parsed is not None
    assert parsed.timestamp is None
    assert parsed.level is None
    assert parsed.message == "just a message with 42 things"


def test_parse_text_timestamp_only_line_is_empty() -> None:
    assert parse_text("2026-09-23T21:12:41Z") is None
    assert parse_text("2026-09-23T21:12:41Z INFO") is None
    assert parse_text("") is None
    assert parse_text("   \t ") is None


def test_parse_text_iso_offset_and_epoch_prefixes() -> None:
    parsed = parse_text("2026-09-23T09:04:07.120-04:00 cache refresh")
    assert parsed is not None
    assert parsed.timestamp == datetime(2026, 9, 23, 13, 4, 7, 120000, tzinfo=UTC)
    assert parsed.message == "cache refresh"
    parsed = parse_text("1790000000 cache refresh")
    assert parsed is not None
    assert parsed.timestamp == datetime.fromtimestamp(1790000000, tz=UTC)
    assert parsed.message == "cache refresh"


def test_parse_text_syslog_prefix_uses_reference_year() -> None:
    reference = datetime(2026, 10, 8, tzinfo=UTC)
    parsed = parse_text("Sep 23 21:12:41 host app[12]: started", reference=reference)
    assert parsed is not None
    assert parsed.timestamp == datetime(2026, 9, 23, 21, 12, 41, tzinfo=UTC)
    assert parsed.message == "host app[12]: started"


def test_parse_text_strips_trailing_newlines_and_collapses_whitespace_edges() -> None:
    parsed = parse_text("  hello world  \r\n")
    assert parsed is not None
    assert parsed.message == "hello world"


@needs_sim
def test_parse_text_every_export_log_line_from_the_simulator() -> None:
    files = sorted((SIM_DIR / "warehouse").glob("export-job-*.log"))
    assert files
    for line in files[0].read_text(encoding="utf-8").splitlines():
        parsed = parse_text(line, NEW_YORK)
        assert parsed is not None, line[:40]
        assert parsed.timestamp is not None
        assert parsed.level in {"INFO", "ERROR", "WARN"}


@settings(max_examples=200, deadline=2000)
@given(st.text(max_size=300))
def test_parse_text_property_never_raises(text: str) -> None:
    result = parse_text(text)
    assert result is None or result.message.strip() == result.message
