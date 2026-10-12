"""tools/ci/push_routes.py: the standard-library OTLP encoder the CI push-route check uses
(ADR 0032) produces requests that the protobuf classes and the gateway's parser both read."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

from carto_edge.gateway.otlp import parse_logs_request

ROOT = Path(__file__).resolve().parents[2]


def _push_routes() -> Any:
    spec = importlib.util.spec_from_file_location(
        "push_routes", ROOT / "tools" / "ci" / "push_routes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_encoded_request_parses_as_otlp() -> None:
    body = _push_routes().otlp_request("src_webstore_log", '{"msg": "hello"}', 1_760_000_000_123)
    request = logs_service_pb2.ExportLogsServiceRequest.FromString(body)
    [resource_logs] = request.resource_logs
    [attribute] = resource_logs.resource.attributes
    assert attribute.key == "carto.source_id"
    assert attribute.value.string_value == "src_webstore_log"
    [record] = resource_logs.scope_logs[0].log_records
    assert record.time_unix_nano == 1_760_000_000_123
    assert record.body.string_value == '{"msg": "hello"}'


def test_the_gateway_accepts_the_encoded_request() -> None:
    body = _push_routes().otlp_request("src_webstore_log", "x" * 300, 1_760_000_000_000_000_000)
    parsed = parse_logs_request(
        body,
        content_encoding="",
        max_bytes=1 << 20,
        received_at=datetime(2026, 10, 12, tzinfo=UTC),
        system_of={"src_webstore_log": "sys_webstore"},
    )
    assert parsed.accepted == 1
    assert parsed.rejected == 0
    [record] = parsed.all_records()
    assert record.text == "x" * 300
    assert record.source_id == "src_webstore_log"
