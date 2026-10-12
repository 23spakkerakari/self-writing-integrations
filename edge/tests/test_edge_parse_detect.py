"""Format auto-detection in spec 8.2 order, first match wins, cached per source."""

from __future__ import annotations

from carto_edge.config import RecordFormat
from carto_edge.pipeline.parse.detect import FormatDetector, sniff

JSON_LINE = '{"ts": "2026-09-23T13:04:06.001Z", "msg": "cart created"}'
XML_LINE = "<heartbeat><status>OK</status></heartbeat>"
LOGFMT_LINE = 'ts=2026-09-23T13:16:46.566Z level=info msg="order created from cart"'
ACCESS_LINE = '10.0.0.5 - - [23/Sep/2026:13:04:06 -0400] "POST /api/v2/carts HTTP/1.1" 503 -'
TEXT_LINE = "2026-09-23 21:12:41 INFO PO export finished: 412 POs written to SHIP_20260923_2112.csv"


def test_sniff_orders_candidates_by_shape_and_always_ends_with_text() -> None:
    assert sniff(JSON_LINE)[0] == RecordFormat.JSON
    assert sniff(XML_LINE)[0] == RecordFormat.XML
    assert sniff(LOGFMT_LINE)[0] == RecordFormat.LOGFMT
    assert sniff(ACCESS_LINE)[0] == RecordFormat.ACCESS_LOG
    assert sniff(TEXT_LINE)[0] == RecordFormat.TEXT
    for line in (JSON_LINE, XML_LINE, LOGFMT_LINE, ACCESS_LINE, TEXT_LINE, ""):
        order = sniff(line)
        assert order[-1] == RecordFormat.TEXT
        assert len(set(order)) == len(order)
        assert RecordFormat.AUTO not in order
        assert RecordFormat.NDJSON not in order


def test_sniff_keeps_the_spec_order_among_candidates() -> None:
    order = sniff('a=1 b=2 c=3 [23/Sep/2026:13:04:06 +0000] "GET / HTTP/1.1" 200 1')
    assert order == (RecordFormat.LOGFMT, RecordFormat.ACCESS_LOG, RecordFormat.TEXT)


def test_sniff_returns_only_plausible_candidates() -> None:
    assert sniff(TEXT_LINE) == (RecordFormat.TEXT,)
    assert sniff(JSON_LINE) == (RecordFormat.JSON, RecordFormat.TEXT)
    assert sniff(XML_LINE) == (RecordFormat.XML, RecordFormat.TEXT)


def locked(detector: FormatDetector) -> RecordFormat | None:
    """Read the lock through a call so mypy does not narrow the property across mutations."""
    return detector.locked


def test_detector_locks_after_consistent_records_and_prefers_the_locked_format() -> None:
    detector = FormatDetector(lock_after=3)
    assert locked(detector) is None
    for _ in range(2):
        detector.observe(RecordFormat.LOGFMT)
    assert locked(detector) is None
    detector.observe(RecordFormat.LOGFMT)
    assert locked(detector) == RecordFormat.LOGFMT
    order = detector.order(JSON_LINE, csv_ready=False)
    assert order[0] == RecordFormat.LOGFMT
    assert order[1] == RecordFormat.JSON
    assert order[-1] == RecordFormat.TEXT


def test_detector_resets_the_streak_on_a_different_format() -> None:
    detector = FormatDetector(lock_after=3)
    detector.observe(RecordFormat.JSON)
    detector.observe(RecordFormat.JSON)
    detector.observe(RecordFormat.TEXT)
    detector.observe(RecordFormat.JSON)
    assert locked(detector) is None
    detector.observe(RecordFormat.JSON)
    detector.observe(RecordFormat.JSON)
    assert locked(detector) == RecordFormat.JSON


def test_detector_relocks_when_a_source_changes_format_for_good() -> None:
    detector = FormatDetector(lock_after=2)
    detector.observe(RecordFormat.JSON)
    detector.observe(RecordFormat.JSON)
    assert locked(detector) == RecordFormat.JSON
    detector.observe(RecordFormat.LOGFMT)
    assert locked(detector) == RecordFormat.JSON
    detector.observe(RecordFormat.LOGFMT)
    assert locked(detector) == RecordFormat.LOGFMT


def test_detector_offers_csv_only_when_a_header_is_known() -> None:
    detector = FormatDetector()
    assert detector.order("1,2,3", csv_ready=False) == (RecordFormat.TEXT,)
    assert detector.order("1,2,3", csv_ready=True) == (RecordFormat.CSV, RecordFormat.TEXT)
    line = 'a=1 b=2 c=3 [23/Sep/2026:13:04:06 +0000] "GET / HTTP/1.1" 200 1'
    assert detector.order(line, csv_ready=True) == (
        RecordFormat.LOGFMT,
        RecordFormat.CSV,
        RecordFormat.ACCESS_LOG,
        RecordFormat.TEXT,
    )


def test_detector_never_puts_text_first() -> None:
    detector = FormatDetector(lock_after=1)
    detector.observe(RecordFormat.TEXT)
    assert detector.locked == RecordFormat.TEXT
    assert detector.order(JSON_LINE, csv_ready=False) == (RecordFormat.JSON, RecordFormat.TEXT)
