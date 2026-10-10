"""carto_edge.gateway.otlp: OTLP/HTTP protobuf log requests to raw records (spec 8.1.2, 2.3
invariant 8; plan M1 wave 2 D2): records grouped by the ``carto.source_id`` resource attribute,
untagged or unknown sources rejected, bodies and attributes mapped to ``text`` and ``fields``,
stable locators, capped decompression, and errors that never carry body content."""

from __future__ import annotations

import base64
import gzip
import hashlib
from datetime import UTC, datetime

import pytest
import zstandard
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

from carto_edge.config import ParseConfig
from carto_edge.gateway.otlp import (
    MAX_ARRAY_ITEMS,
    MAX_LOG_RECORDS,
    MAX_VALUE_DEPTH,
    MAX_WIRE_RECORDS,
    OtlpBodyError,
    OtlpRecords,
    count_log_records,
    parse_logs_request,
)
from carto_edge.pipeline.severity import detect_severity
from carto_edge.pipeline.timestamps import find_timestamp
from carto_schema.event import EventKind, Severity

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
EVENT_NS = 1_791_547_200_123_456_789  # 2026-10-09T12:00:00.123456789Z
SYSTEMS = {"src_web_log": "sys_web", "src_orders_log": "sys_orders"}
CAP = 64 * 1024
MARKER = "mk7f3a9c1e"


def string(text: str) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(string_value=text)


def kv(key: str, value: common_pb2.AnyValue) -> common_pb2.KeyValue:
    return common_pb2.KeyValue(key=key, value=value)


def kvlist(**values: common_pb2.AnyValue) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(
        kvlist_value=common_pb2.KeyValueList(values=[kv(k, v) for k, v in values.items()])
    )


def line(
    text: str,
    *,
    time_ns: int = 0,
    observed_ns: int = 0,
    severity_text: str = "",
    attributes: dict[str, common_pb2.AnyValue] | None = None,
) -> logs_pb2.LogRecord:
    return logs_pb2.LogRecord(
        time_unix_nano=time_ns,
        observed_time_unix_nano=observed_ns,
        severity_text=severity_text,
        body=string(text),
        attributes=[kv(k, v) for k, v in (attributes or {}).items()],
    )


def resource(
    source: str | common_pb2.AnyValue | None, *records: logs_pb2.LogRecord
) -> logs_pb2.ResourceLogs:
    attributes = [kv("host.name", string("web-1"))]
    if isinstance(source, str):
        attributes.append(kv("carto.source_id", string(source)))
    elif source is not None:
        attributes.append(kv("carto.source_id", source))
    return logs_pb2.ResourceLogs(
        resource=resource_pb2.Resource(attributes=attributes),
        scope_logs=[logs_pb2.ScopeLogs(log_records=list(records))],
    )


def body_of(*resources: logs_pb2.ResourceLogs) -> bytes:
    request = logs_service_pb2.ExportLogsServiceRequest(resource_logs=list(resources))
    data: bytes = request.SerializeToString()
    return data


def parse(body: bytes, encoding: str = "", max_bytes: int = CAP) -> OtlpRecords:
    return parse_logs_request(
        body,
        content_encoding=encoding,
        max_bytes=max_bytes,
        received_at=NOW,
        system_of=SYSTEMS,
    )


def test_records_are_grouped_by_source_and_untagged_resources_are_rejected() -> None:
    body = body_of(
        resource("src_web_log", line("GET /cart 200"), line("GET /checkout 500")),
        resource(None, line("untagged one"), line("untagged two"), line("untagged three")),
        resource("src_orders_log", line("order created")),
    )
    result = parse_logs_request(
        body, content_encoding="", max_bytes=CAP, received_at=NOW, system_of=SYSTEMS
    )
    assert result.accepted == 3
    assert result.rejected == 3
    assert sorted(result.records) == ["src_orders_log", "src_web_log"]
    web = result.records["src_web_log"]
    assert [record.text for record in web] == ["GET /cart 200", "GET /checkout 500"]
    assert {record.system_id for record in web} == {"sys_web"}
    assert result.records["src_orders_log"][0].system_id == "sys_orders"
    assert [record.sequence for record in result.all_records()] == [0, 1, 5]


def test_string_body_becomes_text_and_file_attributes_are_ignored() -> None:
    record_pb = line(
        '{"level":"info","msg":"cart created"}',
        attributes={"log.file.name": string("app-2026-10-09.ndjson")},
    )
    result = parse_logs_request(
        body_of(resource("src_web_log", record_pb)),
        content_encoding="identity",
        max_bytes=CAP,
        received_at=NOW,
        system_of=SYSTEMS,
    )
    (record,) = result.records["src_web_log"]
    assert record.text == '{"level":"info","msg":"cart created"}'
    assert record.fields is None  # a bare line keeps the parser's format detection
    assert record.kind is EventKind.LOG
    assert record.received_at == NOW
    assert record.template_hint is None
    assert record.commit_cursor is None
    assert record.size_bytes == record_pb.ByteSize()  # the whole record, attributes included


def test_kvlist_body_becomes_nested_fields_and_attributes_merge() -> None:
    body = kvlist(
        msg=string("payment captured"),
        order=kvlist(id=string("SO-0004471"), lines=common_pb2.AnyValue(int_value=3)),
        amount=common_pb2.AnyValue(double_value=129.99),
        paid=common_pb2.AnyValue(bool_value=True),
        raw=common_pb2.AnyValue(bytes_value=b"\x00\x01\xff"),
        tags=common_pb2.AnyValue(
            array_value=common_pb2.ArrayValue(values=[string("a"), string("b")])
        ),
        empty=common_pb2.AnyValue(),
    )
    record_pb = logs_pb2.LogRecord(
        body=body,
        attributes=[
            kv("http.status_code", common_pb2.AnyValue(int_value=201)),
            kv("msg", string("attribute does not overwrite the body")),
            kv("log.file.path", string("/data/sim/app.log")),
        ],
    )
    result = parse(body_of(resource("src_web_log", record_pb)))
    (record,) = result.records["src_web_log"]
    assert record.text is None
    assert record.fields == {
        "msg": "payment captured",
        "order": {"id": "SO-0004471", "lines": 3},
        "amount": 129.99,
        "paid": True,
        "raw": base64.b64encode(b"\x00\x01\xff").decode("ascii"),
        "tags": ["a", "b"],
        "empty": None,
        "http.status_code": 201,
    }


def test_string_body_with_attributes_carries_text_and_fields() -> None:
    record_pb = line("user signed in", attributes={"user.id": string("u-88123")})
    result = parse(body_of(resource("src_web_log", record_pb)))
    (record,) = result.records["src_web_log"]
    assert record.text == "user signed in"
    assert record.fields == {"user.id": "u-88123"}


def test_severity_and_event_time_are_added_to_structured_records_only() -> None:
    structured = logs_pb2.LogRecord(
        time_unix_nano=EVENT_NS,
        severity_text="WARN",
        body=kvlist(msg=string("stock low")),
    )
    has_level = logs_pb2.LogRecord(
        severity_text="ERROR", body=kvlist(level=string("info"), ts=string("2026-10-09"))
    )
    numbered = logs_pb2.LogRecord(
        severity_number=logs_pb2.SEVERITY_NUMBER_ERROR, body=kvlist(msg=string("refused"))
    )
    bare = line("plain line", time_ns=EVENT_NS, severity_text="INFO")
    result = parse(body_of(resource("src_web_log", structured, has_level, numbered, bare)))
    first, second, third, fourth = result.records["src_web_log"]
    assert first.fields == {
        "msg": "stock low",
        "severity_text": "WARN",
        "timestamp": "2026-10-09T12:00:00.123456Z",
    }
    assert second.fields == {"level": "info", "ts": "2026-10-09"}
    assert third.fields == {"msg": "refused", "severity_number": 17}
    assert fourth.fields is None
    assert fourth.text == "plain line"


def test_the_added_keys_are_the_ones_the_parser_reads() -> None:
    fields = {"severity_text": "WARN", "timestamp": "2026-10-09T11:20:00.123456Z"}
    assert detect_severity(dict(fields), "") is Severity.WARN
    found = find_timestamp(fields, ParseConfig())
    assert found is not None
    assert found[1] == "timestamp"
    assert found[0] == datetime(2026, 10, 9, 11, 20, 0, 123456, tzinfo=UTC)


def test_locator_is_stable_and_depends_on_source_body_and_time() -> None:
    record_pb = line("GET /cart 200", time_ns=EVENT_NS)
    body = body_of(resource("src_web_log", record_pb), resource("src_orders_log", record_pb))
    first = parse(body)
    again = parse(body)
    web = first.records["src_web_log"][0]
    orders = first.records["src_orders_log"][0]
    assert web.locator == again.records["src_web_log"][0].locator
    expected = hashlib.sha256(
        b"src_web_log\x00" + record_pb.body.SerializeToString() + b"\x00" + str(EVENT_NS).encode()
    ).hexdigest()[:16]
    assert web.locator == f"otlp:{expected}"
    assert orders.locator != web.locator
    later = parse(body_of(resource("src_web_log", line("GET /cart 200", time_ns=EVENT_NS + 1))))
    assert later.records["src_web_log"][0].locator != web.locator


def test_locator_falls_back_to_the_observed_time_when_the_event_time_is_unset() -> None:
    one = line("heartbeat ok", observed_ns=EVENT_NS)
    two = line("heartbeat ok", observed_ns=EVENT_NS + 1000)
    result = parse(body_of(resource("src_web_log", one, two)))
    first, second = result.records["src_web_log"]
    assert first.locator != second.locator
    expected = hashlib.sha256(
        b"src_web_log\x00" + one.body.SerializeToString() + b"\x00" + str(EVENT_NS).encode()
    ).hexdigest()[:16]
    assert first.locator == f"otlp:{expected}"


def test_values_are_capped_in_depth_and_array_length() -> None:
    nested = string("bottom")
    for _ in range(MAX_VALUE_DEPTH + 5):
        nested = kvlist(child=nested)
    many = common_pb2.AnyValue(
        array_value=common_pb2.ArrayValue(
            values=[common_pb2.AnyValue(int_value=n) for n in range(MAX_ARRAY_ITEMS + 10)]
        )
    )
    record_pb = logs_pb2.LogRecord(body=kvlist(deep=nested, many=many))
    result = parse(body_of(resource("src_web_log", record_pb)))
    (record,) = result.records["src_web_log"]
    assert record.fields is not None
    assert record.fields["many"] == list(range(MAX_ARRAY_ITEMS))
    depth = 1
    current = record.fields["deep"]
    while isinstance(current, dict):
        depth += 1
        current = current["child"]
    assert depth == MAX_VALUE_DEPTH
    assert current is None


def test_unknown_and_non_string_source_ids_are_rejected() -> None:
    body = body_of(
        resource("src_not_configured", line("a")),
        resource(common_pb2.AnyValue(int_value=7), line("b"), line("c")),
        resource("", line("d")),
        resource("src_web_log", line("e")),
    )
    result = parse(body)
    assert result.accepted == 1
    assert result.rejected == 4
    assert list(result.records) == ["src_web_log"]


def test_gzip_and_zstd_bodies_are_decoded() -> None:
    body = body_of(resource("src_web_log", line("GET /cart 200"), line("GET /cart 404")))
    for encoding, data in (
        ("gzip", gzip.compress(body)),
        ("GZIP", gzip.compress(body)),
        ("zstd", zstandard.ZstdCompressor().compress(body)),
        ("gzip", gzip.compress(body[:10]) + gzip.compress(body[10:])),
    ):
        result = parse(data, encoding)
        assert result.accepted == 2, encoding


def test_decompressed_size_is_capped() -> None:
    bomb = b"\x00" * (CAP * 20)
    for encoding, data in (
        ("gzip", gzip.compress(bomb)),
        ("zstd", zstandard.ZstdCompressor().compress(bomb)),
        ("", bomb),
    ):
        with pytest.raises(OtlpBodyError) as caught:
            parse(data, encoding)
        assert caught.value.reason == "too_large"


def test_bad_bodies_raise_typed_errors_without_content() -> None:
    marked = MARKER.encode() * 20
    cases = [
        ("gzip", marked, "bad_encoding"),
        ("gzip", gzip.compress(marked)[:-12], "bad_encoding"),
        ("zstd", marked, "bad_encoding"),
        ("br", body_of(), "bad_encoding"),
        ("", b"\xff\xff\xff" + marked, "bad_protobuf"),
    ]
    for encoding, data, reason in cases:
        with pytest.raises(OtlpBodyError) as caught:
            parse(data, encoding)
        assert caught.value.reason == reason, (encoding, reason)
        assert MARKER not in str(caught.value)


def test_an_empty_request_is_accepted_with_nothing_in_it() -> None:
    result = parse(b"")
    assert result.accepted == 0
    assert result.rejected == 0
    assert result.all_records() == []


def test_records_per_request_are_capped() -> None:
    extra = 7
    records = [line(f"l{n}") for n in range(MAX_LOG_RECORDS + extra)]
    body = body_of(
        resource("src_web_log", *records[:6000]), resource("src_orders_log", *records[6000:])
    )
    result = parse(body, max_bytes=4 * 1024 * 1024)
    assert result.accepted == MAX_LOG_RECORDS
    assert result.rejected == extra
    assert len(result.records["src_orders_log"]) == MAX_LOG_RECORDS - 6000


def test_the_wire_is_counted_before_parsing_and_floods_are_refused() -> None:
    """Two-byte empty records would expand to millions of parsed objects (review finding)."""
    body = body_of(resource("src_web_log", line("a"), line("b")), resource(None, line("c")))
    assert count_log_records(body, 10) == 3
    flood = logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                scope_logs=[
                    logs_pb2.ScopeLogs(
                        log_records=[logs_pb2.LogRecord() for _ in range(MAX_WIRE_RECORDS + 1)]
                    )
                ]
            )
        ]
    ).SerializeToString()
    assert len(flood) < CAP * 4
    with pytest.raises(OtlpBodyError) as caught:
        parse(flood, max_bytes=CAP * 4)
    assert caught.value.reason == "too_many"
    with pytest.raises(OtlpBodyError) as truncated:
        count_log_records(body[:-3], 10)
    assert truncated.value.reason == "bad_protobuf"


def test_size_bytes_counts_attributes_too() -> None:
    """The parser's max_record_bytes must apply to attributes, not only to the body."""
    big = "x" * 50_000
    records = parse(body_of(resource("src_web_log", line("", attributes={"big": string(big)}))))
    record = records.records["src_web_log"][0]
    assert record.size_bytes > len(big)
