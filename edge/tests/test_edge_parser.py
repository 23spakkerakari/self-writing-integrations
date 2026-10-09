"""The parse stage end to end: RawRecord in, ParsedRecord or ParseFailure out (spec 8.2, 5.4
step 3, ADR 0016)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from carto_edge.config import ParseConfig, RecordFormat, SourceConfig, SourceType
from carto_edge.pipeline.model import ParsedRecord, RawRecord
from carto_edge.pipeline.parser import ParseFailure, RecordParser
from carto_edge.pipeline.templates import MASK, TemplateStore, compute_template_id
from carto_schema.event import EventKind, ObservedAtQuality, Severity

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)
RECEIVED = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def source(system: str = "sys_test", **parse: Any) -> SourceConfig:
    return SourceConfig(
        id="src_test", system=system, type=SourceType.UPLOAD, parse=ParseConfig(**parse)
    )


def raw(
    text: str | None = None,
    fields: dict[str, Any] | None = None,
    *,
    kind: EventKind = EventKind.LOG,
    locator: str = "f.log:line:1",
    template_hint: str | None = None,
    size_bytes: int = 0,
) -> RawRecord:
    return RawRecord(
        source_id="src_test",
        system_id="sys_test",
        kind=kind,
        locator=locator,
        received_at=RECEIVED,
        text=text,
        fields=fields,
        template_hint=template_hint,
        size_bytes=size_bytes,
    )


def parsed(result: ParsedRecord | ParseFailure) -> ParsedRecord:
    assert isinstance(result, ParsedRecord), result
    return result


def test_parser_webstore_ndjson_line() -> None:
    parser = RecordParser(source(), TemplateStore())
    line = (
        '{"ts": "2026-09-23T13:04:06.001Z", "level": "info", "msg": "cart created", '
        '"cart_id": "c-88213", "items": 3, "region": "us-east", "channel": "web"}'
    )
    record = parsed(parser.parse(raw(line)))
    assert record.kind == EventKind.LOG
    assert record.observed_at == datetime(2026, 9, 23, 13, 4, 6, 1000, tzinfo=UTC)
    assert record.observed_at_quality == ObservedAtQuality.SOURCE
    assert record.template_text == "cart created"
    assert record.template_id == compute_template_id("sys_test", "cart created")
    assert record.severity == Severity.INFO
    assert record.fields == {
        "cart_id": "c-88213",
        "items": "3",
        "region": "us-east",
        "channel": "web",
    }
    assert record.parse_format == "json"
    assert record.source_id == "src_test"
    assert record.locator == "f.log:line:1"
    assert record.received_at == RECEIVED


def test_parser_message_params_become_msg_param_fields() -> None:
    parser = RecordParser(source(), TemplateStore())
    first = parsed(
        parser.parse(raw('{"ts": "2026-09-23T13:04:06Z", "msg": "user alice logged in"}'))
    )
    assert first.template_text == "user alice logged in"
    second = parsed(
        parser.parse(raw('{"ts": "2026-09-23T13:04:07Z", "msg": "user bob logged in"}'))
    )
    assert second.template_text == f"user {MASK} logged in"
    assert second.fields == {"msg.param_0": "bob"}
    assert "msg" not in second.fields


def test_parser_structured_record_without_message_uses_key_signature() -> None:
    parser = RecordParser(source(), TemplateStore())
    record = parsed(parser.parse(raw('{"ts": "2026-09-23T13:04:06Z", "b": 1, "a": {"c": 2}}')))
    assert record.template_text == "keys:a,b,ts"
    assert record.fields == {"b": "1", "a.c": "2"}


def test_parser_configured_message_field_and_actor_field() -> None:
    parser = RecordParser(source(message_field="event", actor_field="user"), TemplateStore())
    record = parsed(parser.parse(raw('{"event": "login ok 7", "user": "alice", "msg": "kept"}')))
    assert record.template_text == f"login ok {MASK}"
    assert record.fields == {"msg.param_0": "7", "user": "alice", "msg": "kept"}
    assert record.actor == "alice"


def test_parser_orders_logfmt_line_with_error_status() -> None:
    parser = RecordParser(source("sys_orders"), TemplateStore())
    line = (
        'ts=2026-09-28T14:20:19.579Z level=error msg="payment request failed" order_id=7816 '
        "merchant_ref=X9-3787 http_status=503"
    )
    record = parsed(parser.parse(raw(line)))
    assert record.parse_format == "logfmt"
    assert record.severity == Severity.ERROR
    assert record.template_text == "payment request failed"
    assert record.fields == {"order_id": "7816", "merchant_ref": "X9-3787", "http_status": "503"}
    assert record.observed_at == datetime(2026, 9, 28, 14, 20, 19, 579000, tzinfo=UTC)


def test_parser_payments_xml_root_is_the_template_and_http_status_sets_severity() -> None:
    parser = RecordParser(source("sys_payments"), TemplateStore())
    failed = (
        "<paymentMessage><timestamp>2026-09-28T10:20:19.186-04:00</timestamp>"
        "<merchantRef>X9-3787</merchantRef><amount>596.99</amount><currency>USD</currency>"
        "<status>ERROR</status><httpStatus>503</httpStatus><error>Service Unavailable</error>"
        "<processor>cardnet</processor></paymentMessage>"
    )
    record = parsed(parser.parse(raw(failed)))
    assert record.parse_format == "xml"
    assert record.template_text == "paymentMessage"
    assert record.template_id == compute_template_id("sys_test", "paymentMessage")
    assert record.severity == Severity.ERROR
    assert record.observed_at == datetime(2026, 9, 28, 14, 20, 19, 186000, tzinfo=UTC)
    assert "timestamp" not in record.fields
    assert record.fields["merchantRef"] == "X9-3787"
    assert record.fields["error"] == "Service Unavailable"
    heartbeat = parsed(
        parser.parse(
            raw(
                "<heartbeat><timestamp>2026-09-23T00:23:06.570-04:00</timestamp><status>OK</status></heartbeat>"
            )
        )
    )
    assert heartbeat.template_text == "heartbeat"
    assert heartbeat.severity is None
    assert heartbeat.fields == {"status": "OK"}
    assert heartbeat.template_id != record.template_id


def test_parser_export_log_text_lines_with_local_zone() -> None:
    parser = RecordParser(
        source("sys_warehouse", format=RecordFormat.TEXT, timezone="America/New_York"),
        TemplateStore(),
    )
    record = parsed(
        parser.parse(
            raw(
                "2026-09-23 21:12:41 INFO PO export finished: 412 POs written to "
                "SHIP_20260923_2112.csv"
            )
        )
    )
    assert record.parse_format == "text"
    assert record.template_text == f"PO export finished: {MASK} POs written to {MASK}"
    assert record.fields == {"param_0": "412", "param_1": "SHIP_20260923_2112.csv"}
    assert record.severity == Severity.INFO
    assert record.observed_at == datetime(2026, 9, 24, 1, 12, 41, tzinfo=UTC)
    assert record.observed_at_quality == ObservedAtQuality.SOURCE
    error = parsed(
        parser.parse(
            raw(
                "2026-10-01 21:14:30 ERROR SFTP upload failed: Permission denied "
                "(/outbound/shipping/SHIP_20261001_2113.csv)"
            )
        )
    )
    assert error.severity == Severity.ERROR
    assert error.template_text == f"SFTP upload failed: Permission denied {MASK}"
    assert error.fields == {"param_0": "(/outbound/shipping/SHIP_20261001_2113.csv)"}


def test_parser_text_without_level_uses_the_error_lexicon() -> None:
    parser = RecordParser(source(format=RecordFormat.TEXT), TemplateStore())
    record = parsed(parser.parse(raw("2026-09-23T21:12:41Z connection refused by 10.0.0.1")))
    assert record.severity == Severity.ERROR
    record = parsed(parser.parse(raw("2026-09-23T21:12:41Z connection established to 10.0.0.1")))
    assert record.severity is None


def test_parser_access_log_sets_kind_and_template() -> None:
    parser = RecordParser(source(), TemplateStore())
    line = (
        '203.0.113.9 - - [23/Sep/2026:13:04:06 +0000] "GET /orders/4471/items HTTP/1.1" 503 512 '
        '"-" "curl/8.0"'
    )
    record = parsed(parser.parse(raw(line)))
    assert record.kind == EventKind.HTTP_ACCESS
    assert record.parse_format == "access_log"
    assert record.template_text == "GET /orders/*/items 5xx"
    assert record.severity == Severity.ERROR
    assert record.observed_at == datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)
    assert "time" not in record.fields
    assert record.fields["path"] == "/orders/4471/items"
    assert record.fields["status"] == "503"
    assert record.fields["user_agent"] == "curl/8.0"
    assert "referer" not in record.fields


def test_parser_access_log_custom_pattern() -> None:
    parser = RecordParser(
        source(
            format=RecordFormat.ACCESS_LOG,
            access_log_pattern=(
                r"^(?P<time>\S+) (?P<method>[A-Z]+) (?P<path>\S+) (?P<status>\d{3})$"
            ),
        ),
        TemplateStore(),
    )
    record = parsed(parser.parse(raw("2026-09-23T13:04:06Z GET /u/42 404")))
    assert record.kind == EventKind.HTTP_ACCESS
    assert record.template_text == "GET /u/* 4xx"
    assert record.severity == Severity.WARN
    assert record.observed_at == datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)
    assert isinstance(parser.parse(raw("garbage")), ParseFailure)


def test_parser_csv_file_with_header_then_rows_then_a_new_file() -> None:
    parser = RecordParser(
        source("sys_warehouse", format=RecordFormat.CSV, timezone="America/New_York"),
        TemplateStore(),
    )
    header = "id,po_num,order_ref,status,warehouse_code,created_by,created_at"
    row = "1,88-210,SO-0004471,SHIPPED,DC-01,svc_wms_integration,2026-09-23 09:21:04"
    first = parser.parse(raw(header, locator="purchase_orders.csv:line:1"))
    assert first == ParseFailure("csv_header")
    record = parsed(parser.parse(raw(row, locator="purchase_orders.csv:line:2")))
    assert record.parse_format == "csv"
    assert record.template_text == (
        "keys:created_at,created_by,id,order_ref,po_num,status,warehouse_code"
    )
    assert record.observed_at == datetime(2026, 9, 23, 13, 21, 4, tzinfo=UTC)
    assert record.fields["po_num"] == "88-210"
    assert "created_at" not in record.fields
    renamed = header.replace("po_num", "po_number")
    assert parser.parse(raw(renamed, locator="purchase_orders.renamed.csv:line:1")) == ParseFailure(
        "csv_header"
    )
    record = parsed(parser.parse(raw(row, locator="purchase_orders.renamed.csv:line:2")))
    assert record.fields["po_number"] == "88-210"
    assert "po_num" not in record.fields


def test_parser_csv_auto_detected_from_a_header_line() -> None:
    parser = RecordParser(source(), TemplateStore())
    assert parser.parse(raw("po_num,carrier", locator="SHIP_1.csv:line:1")) == ParseFailure(
        "csv_header"
    )
    record = parsed(parser.parse(raw("88-210,UPS", locator="SHIP_1.csv:line:2")))
    assert record.parse_format == "csv"
    assert record.fields == {"po_num": "88-210", "carrier": "UPS"}
    assert record.observed_at == RECEIVED
    assert record.observed_at_quality == ObservedAtQuality.INGEST


def test_parser_fields_records_rows_and_file_events_use_the_hint() -> None:
    parser = RecordParser(source("sys_warehouse", timezone="America/New_York"), TemplateStore())
    row = {
        "id": 1,
        "po_num": "88-210",
        "order_ref": "SO-0004471",
        "status": "SHIPPED",
        "order_total": 129.99,
        "created_by": "svc_wms_integration",
        "created_at": datetime(2026, 9, 23, 9, 21, 4),  # naive, as a DB driver returns it
        "updated_at": "2026-09-23 21:41:31",
        "nothing": None,
    }
    record = parsed(
        parser.parse(
            raw(
                fields=row,
                kind=EventKind.ROW_CHANGE,
                locator="purchase_orders:row:1",
                template_hint="row_change purchase_orders",
            )
        )
    )
    assert record.kind == EventKind.ROW_CHANGE
    assert record.parse_format == "fields"
    assert record.template_text == "row_change purchase_orders"
    assert record.observed_at == datetime(2026, 9, 23, 13, 21, 4, tzinfo=UTC)
    assert record.fields == {
        "id": "1",
        "po_num": "88-210",
        "order_ref": "SO-0004471",
        "status": "SHIPPED",
        "order_total": "129.99",
        "created_by": "svc_wms_integration",
        "updated_at": "2026-09-23 21:41:31",
    }
    file_event = parsed(
        parser.parse(
            raw(
                fields={
                    "file_name": "SHIP_20260923_2112.csv",
                    "size": 23891,
                    "mtime": "2026-09-24T01:20:00Z",
                },
                kind=EventKind.FILE_ARRIVED,
                locator="file:SHIP_20260923_2112.csv",
                template_hint="file_arrived SHIP_*_*.csv",
            )
        )
    )
    assert file_event.template_text == "file_arrived SHIP_*_*.csv"
    assert file_event.observed_at == RECEIVED
    assert file_event.observed_at_quality == ObservedAtQuality.INGEST


def test_parser_fields_with_text_mines_the_text_as_the_message() -> None:
    parser = RecordParser(source(), TemplateStore())
    record = parsed(
        parser.parse(
            raw(
                "job 42 finished",
                fields={"ts": "2026-09-23T13:04:06Z", "severity_text": "WARN", "host": "h1"},
                kind=EventKind.LOG,
                locator="otlp:abc",
            )
        )
    )
    assert record.template_text == f"job {MASK} finished"
    assert record.fields == {"host": "h1", "msg.param_0": "42"}
    assert record.severity == Severity.WARN


def test_parser_webhook_without_message_uses_key_signature() -> None:
    parser = RecordParser(source(), TemplateStore())
    record = parsed(
        parser.parse(
            raw(
                fields={"type": "order.paid", "data": {"id": 9}},
                kind=EventKind.WEBHOOK,
                locator="webhook:1",
            )
        )
    )
    assert record.template_text == "keys:data,type"
    assert record.fields == {"type": "order.paid", "data.id": "9"}


def test_parser_missing_timestamp_falls_back_to_ingest_time() -> None:
    parser = RecordParser(source(), TemplateStore())
    record = parsed(parser.parse(raw('{"msg": "no clock here"}')))
    assert record.observed_at == RECEIVED
    assert record.observed_at_quality == ObservedAtQuality.INGEST
    record = parsed(parser.parse(raw('{"ts": "yesterday", "msg": "bad clock"}')))
    assert record.observed_at_quality == ObservedAtQuality.INGEST
    assert record.fields == {"ts": "yesterday"}


def test_parser_received_at_is_converted_to_utc() -> None:
    parser = RecordParser(source(), TemplateStore())
    local = RECEIVED.astimezone(timezone(timedelta(hours=5)))
    record = parsed(
        parser.parse(
            RawRecord(
                source_id="src_test",
                system_id="sys_test",
                kind=EventKind.LOG,
                locator="x:line:1",
                received_at=local,
                text='{"msg": "x"}',
            )
        )
    )
    assert record.observed_at == RECEIVED
    assert record.observed_at.tzinfo is UTC


def test_parser_failures_are_values_not_exceptions() -> None:
    parser = RecordParser(source(), TemplateStore())
    assert parser.parse(raw("")) == ParseFailure("empty")
    assert parser.parse(raw("   \n")) == ParseFailure("empty")
    assert parser.parse(raw(None)) == ParseFailure("empty")
    assert parser.parse(raw(fields={})) == ParseFailure("empty")
    assert parser.parse(raw(fields={"a": None})) == ParseFailure("empty")
    strict = RecordParser(source(format=RecordFormat.JSON), TemplateStore())
    assert strict.parse(raw("not json")) == ParseFailure("unparseable")
    strict = RecordParser(source(format=RecordFormat.XML), TemplateStore())
    assert strict.parse(raw("<a>")) == ParseFailure("unparseable")
    strict = RecordParser(source(format=RecordFormat.LOGFMT), TemplateStore())
    assert strict.parse(raw("no pairs here")) == ParseFailure("unparseable")


def test_parser_size_limit_is_enforced_before_parsing() -> None:
    parser = RecordParser(source(max_record_bytes=256), TemplateStore())
    big = json.dumps({"msg": "x" * 300})
    assert parser.parse(raw(big)) == ParseFailure("too_large")
    assert parser.parse(raw('{"msg": "x"}', size_bytes=1000)) == ParseFailure("too_large")
    assert parser.parse(raw(fields={"a": 1}, size_bytes=1000)) == ParseFailure("too_large")
    multibyte = '{"msg": "' + "é" * 130 + '"}'
    assert parser.parse(raw(multibyte)) == ParseFailure("too_large")


def test_parser_auto_mode_falls_back_across_formats_per_line() -> None:
    parser = RecordParser(source(), TemplateStore())
    assert parsed(parser.parse(raw('{"msg": "a"}'))).parse_format == "json"
    assert parsed(parser.parse(raw("ts=1 msg=b"))).parse_format == "logfmt"
    assert parsed(parser.parse(raw("<r><m>c</m></r>"))).parse_format == "xml"
    assert parsed(parser.parse(raw("plain words"))).parse_format == "text"
    for _ in range(10):
        parsed(parser.parse(raw('{"msg": "a"}')))
    assert parser.detected_format == RecordFormat.JSON
    assert parsed(parser.parse(raw("plain words"))).parse_format == "text"


def test_parser_configured_format_is_strict() -> None:
    parser = RecordParser(source(format=RecordFormat.NDJSON), TemplateStore())
    assert parsed(parser.parse(raw('{"msg": "a"}'))).parse_format == "ndjson"
    assert parser.parse(raw("ts=1 msg=b")) == ParseFailure("unparseable")


def test_parser_registers_templates_with_kind_and_seen_range() -> None:
    store = TemplateStore()
    parser = RecordParser(source(), store)
    parsed(parser.parse(raw('{"ts": "2026-09-23T13:04:06Z", "msg": "cart created"}')))
    parsed(parser.parse(raw('{"ts": "2026-09-23T13:05:06Z", "msg": "cart created"}')))
    parsed(
        parser.parse(
            raw(
                '203.0.113.9 - - [23/Sep/2026:13:04:06 +0000] "GET /x HTTP/1.1" 200 1',
            )
        )
    )
    records = {r.template_text: r for r in store.registry()}
    assert records["cart created"].count == 2
    assert records["cart created"].kind == EventKind.LOG
    assert records["cart created"].first_seen == datetime(2026, 9, 23, 13, 4, 6, tzinfo=UTC)
    assert records["cart created"].last_seen == datetime(2026, 9, 23, 13, 5, 6, tzinfo=UTC)
    assert records["GET /x 2xx"].kind == EventKind.HTTP_ACCESS


def test_parser_unknown_timezone_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="timezone"):
        RecordParser(source(timezone="Mars/Olympus"), TemplateStore())


def test_parser_notes_record_limits_hit() -> None:
    parser = RecordParser(source(), TemplateStore())
    record = parsed(parser.parse(raw(json.dumps({"msg": "x", "items": list(range(40))}))))
    assert "array_limit" in record.parse_notes


@needs_sim
def test_parser_every_simulator_source_parses_with_source_timestamps() -> None:
    specs = [
        ("sys_webstore", "webstore", "app-*.ndjson", {}, "json"),
        ("sys_orders", "orders", "order-svc-*.log", {}, "logfmt"),
        ("sys_payments", "payments", "messages-*.xml", {}, "xml"),
        ("sys_shipping", "shipping", "shipping-app-*.log", {}, "logfmt"),
        (
            "sys_warehouse",
            "warehouse",
            "export-job-*.log",
            {"timezone": "America/New_York"},
            "text",
        ),
    ]
    for system, folder, pattern, extra, expected_format in specs:
        files = sorted((SIM_DIR / folder).glob(pattern))
        assert files, folder
        parser = RecordParser(source(system, **extra), TemplateStore())
        lines = files[0].read_text(encoding="utf-8").splitlines()[:300]
        for number, line in enumerate(lines, start=1):
            record = parsed(parser.parse(raw(line, locator=f"{files[0].name}:line:{number}")))
            assert record.parse_format == expected_format, (folder, line[:40])
            assert record.observed_at_quality == ObservedAtQuality.SOURCE, (folder, line[:40])
            assert record.template_text
            assert not any(c.isdigit() for c in record.template_text) or folder == "payments", (
                folder,
                record.template_text,
            )


@needs_sim
def test_parser_simulator_csv_sources_with_the_rename() -> None:
    parser = RecordParser(
        source("sys_warehouse", format=RecordFormat.CSV, timezone="America/New_York"),
        TemplateStore(),
    )
    seen_po_fields: set[str] = set()
    for name in ("purchase_orders.csv", "purchase_orders.renamed.csv"):
        path = SIM_DIR / "warehouse" / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines()[:100], start=1):
            result = parser.parse(raw(line, locator=f"{name}:line:{number}"))
            if number == 1:
                assert result == ParseFailure("csv_header")
                continue
            record = parsed(result)
            assert record.observed_at_quality == ObservedAtQuality.SOURCE
            seen_po_fields.update(k for k in record.fields if k.startswith("po_num"))
    assert "po_num" in seen_po_fields


@pytest.mark.slow
def test_parser_ndjson_throughput_floor() -> None:
    parser = RecordParser(source("sys_webstore"), TemplateStore())
    lines = [
        json.dumps(
            {
                "ts": f"2026-09-23T13:04:{i % 60:02d}.001Z",
                "level": "info",
                "msg": "cart created",
                "cart_id": f"c-{88213 + i}",
                "items": i % 7,
                "region": "us-east",
                "channel": "web",
            }
        )
        for i in range(20_000)
    ]
    records = [raw(line, locator=f"app.ndjson:line:{i}") for i, line in enumerate(lines)]
    for record in records[:1000]:
        parser.parse(record)
    best = 0.0
    for _ in range(3):  # best of three passes: a loaded CI box must not fail this
        start = time.perf_counter()
        for record in records:
            assert isinstance(parser.parse(record), ParsedRecord)
        best = max(best, len(records) / (time.perf_counter() - start))
    assert best > 5_000, f"{best:.0f} records/s"
