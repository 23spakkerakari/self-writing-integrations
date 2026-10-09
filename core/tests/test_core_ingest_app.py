"""ingest-api (spec 5.3, 8.5, 12, 14.12): accepts the contract models only, is idempotent by
batch id through the ledger (ADR 0015), answers RFC 7807 problems without echoing input, caps
bodies, takes zstd, records heartbeats, exposes metrics, echoes request ids and never logs a
body."""

from __future__ import annotations

import io
import json
import re
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import zstandard

with warnings.catch_warnings():
    # starlette 1.7 deprecates httpx (vs httpx2) under its TestClient at import time; the
    # workspace pins httpx and adding httpx2 is a founder decision, so the one-off is scoped.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_common.ids import ULID_PATTERN, new_ulid
from carto_common.logging import configure_logging
from carto_core.db.clickhouse import WriterError, WriteResult
from carto_core.ingest.app import PROBLEM_CONTENT_TYPE, create_app
from carto_core.ingest.health import InMemorySourceHealthStore
from carto_core.ingest.ledger import InMemoryBatchLedger, LedgerEntry, LedgerError
from carto_core.settings import CoreSettings, IngestListenerSettings
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch, SourceHeartbeat

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
KIB = 1024
JSON = {"content-type": "application/json"}
EXAMPLE_TOKENS = [identifier.token for identifier in CanonicalEvent.example().identifiers]


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[list[CanonicalEvent]] = []
        self.fail = False
        self.ready = True

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        if self.fail:
            raise WriterError("ClickHouse insert failed: DatabaseError")
        self.batches.append(list(events))
        return WriteResult(len(events), sum(len(event.identifiers) for event in events))

    def ping(self) -> bool:
        return self.ready


class FailingLedger(InMemoryBatchLedger):
    def record(self, entry: LedgerEntry) -> bool:
        raise LedgerError("ledger unavailable: OperationalError")


@dataclass
class World:
    client: TestClient
    writer: FakeWriter
    ledger: InMemoryBatchLedger
    health: InMemorySourceHealthStore


def _settings(**ingest: Any) -> CoreSettings:
    return CoreSettings(ingest=IngestListenerSettings(**ingest))


def _world(
    settings: CoreSettings | None = None, ledger: InMemoryBatchLedger | None = None
) -> World:
    writer = FakeWriter()
    ledger = ledger if ledger is not None else InMemoryBatchLedger()
    health = InMemorySourceHealthStore()
    app = create_app(settings or _settings(), writer, ledger, health, clock=lambda: NOW)
    return World(TestClient(app), writer, ledger, health)


@pytest.fixture
def world() -> World:
    return _world()


def _event(index: int = 0) -> CanonicalEvent:
    suffix = "ABCDEFGHJKMNPQRSTVWXYZ"[index]
    data = CanonicalEvent.example().model_dump() | {
        "event_id": f"01J9ZK8X5Q8V3N6M2T4R7W1Y0{suffix}"
    }
    return CanonicalEvent.model_validate(data)


def _payload(events: Sequence[CanonicalEvent] | None = None, **overrides: Any) -> dict[str, Any]:
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=NOW,
        events=list(events) if events is not None else [_event()],
    )
    data: dict[str, Any] = json.loads(batch.model_dump_json())
    return data | overrides


def _heartbeat(**overrides: Any) -> dict[str, Any]:
    heartbeat = SourceHeartbeat(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        sent_at=NOW,
        status="degraded",
        last_success_at=NOW,
        lag_seconds=12.5,
        error_count=2,
        buffer_depth=40,
        oldest_buffered_at=None,
        message="splunk export slow",
    )
    data: dict[str, Any] = json.loads(heartbeat.model_dump_json())
    return data | overrides


def _post(world: World, path: str, payload: dict[str, Any], **kwargs: Any) -> Any:
    return world.client.post(path, content=json.dumps(payload).encode(), headers=JSON, **kwargs)


def _assert_problem(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["title"]
    assert body["request_id"] == response.headers["x-request-id"]
    return body


# --- accepting batches ---------------------------------------------------------------------------


def test_valid_batch_is_accepted_and_written_once(world: World) -> None:
    payload = _payload([_event(0), _event(1)])
    response = _post(world, "/internal/ingest", payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2, "duplicate": False}
    assert response.headers["content-type"].startswith("application/json")
    assert len(world.writer.batches) == 1
    assert [event.event_id for event in world.writer.batches[0]] == [
        payload["events"][0]["event_id"],
        payload["events"][1]["event_id"],
    ]
    assert world.ledger.contains("default", payload["batch_id"])
    (entry,) = world.ledger.entries
    assert entry == LedgerEntry("default", payload["batch_id"], "src_wms_db", 2, NOW)


def test_same_batch_id_twice_is_a_duplicate_and_not_rewritten(world: World) -> None:
    payload = _payload()
    assert _post(world, "/internal/ingest", payload).status_code == 200
    again = _post(world, "/internal/ingest", payload)
    assert again.status_code == 200
    assert again.json() == {"accepted": 1, "duplicate": True}
    assert len(world.writer.batches) == 1
    assert len(world.ledger.entries) == 1
    metrics = world.client.get("/metrics").text
    assert "carto_ingest_duplicates_total 1\n" in metrics
    assert "carto_ingest_batches_total 1\n" in metrics


def test_the_order_is_ledger_lookup_then_write_then_ledger_insert(world: World) -> None:
    """ADR 0015: a failed write leaves no ledger row, so the edge's retry is not a duplicate."""
    payload = _payload()
    world.writer.fail = True
    body = _assert_problem(_post(world, "/internal/ingest", payload), 503)
    assert "retry" in body["detail"]
    assert not world.ledger.contains("default", payload["batch_id"])
    assert world.writer.batches == []
    world.writer.fail = False
    retry = _post(world, "/internal/ingest", payload)
    assert retry.status_code == 200
    assert retry.json() == {"accepted": 1, "duplicate": False}
    assert len(world.writer.batches) == 1


def test_ledger_failure_after_the_write_is_503_so_the_edge_retries() -> None:
    world = _world(ledger=FailingLedger())
    payload = _payload()
    _assert_problem(_post(world, "/internal/ingest", payload), 503)
    assert len(world.writer.batches) == 1, "written once; the retry writes again (ADR 0015)"
    assert world.ledger.entries == ()


# --- rejecting what is not the contract --------------------------------------------------


def test_events_of_another_source_are_rejected_without_echoing_input(world: World) -> None:
    payload = _payload()
    payload["events"][0]["source_id"] = "src_other"
    response = _post(world, "/internal/ingest", payload)
    body = _assert_problem(response, 422)
    assert body["errors"], body
    assert all(set(error) == {"loc", "msg", "type"} for error in body["errors"])
    text = response.text
    assert "input" not in json.dumps(body["errors"])
    for token in EXAMPLE_TOKENS:
        assert token not in text
    assert "CREATED" not in text and "DC-03" not in text
    assert world.writer.batches == []
    assert world.ledger.entries == ()


def test_a_raw_value_in_an_identifier_slot_is_rejected(world: World) -> None:
    payload = _payload()
    payload["events"][0]["identifiers"][0]["token"] = "SO-0004471"  # noqa: S105 - planted
    body = _assert_problem(_post(world, "/internal/ingest", payload), 422)
    assert "SO-0004471" not in json.dumps(body)
    assert world.writer.batches == []


def test_unknown_keys_and_empty_batches_are_rejected(world: World) -> None:
    _assert_problem(_post(world, "/internal/ingest", _payload(extra="x")), 422)
    _assert_problem(_post(world, "/internal/ingest", _payload() | {"events": []}), 422)


def test_malformed_json_is_a_400_problem(world: World) -> None:
    response = world.client.post("/internal/ingest", content=b"{not json", headers=JSON)
    body = _assert_problem(response, 400)
    assert "not json" not in json.dumps(body)


def test_empty_body_is_a_400_problem(world: World) -> None:
    _assert_problem(world.client.post("/internal/ingest", content=b"", headers=JSON), 400)


def test_other_tenant_is_refused(world: World) -> None:
    payload = _payload()
    payload["tenant_id"] = "other"
    payload["events"][0]["tenant_id"] = "other"
    _assert_problem(_post(world, "/internal/ingest", payload), 403)
    assert world.writer.batches == []


def test_wrong_media_type_and_unsupported_encoding_are_415(world: World) -> None:
    raw = json.dumps(_payload()).encode()
    _assert_problem(
        world.client.post("/internal/ingest", content=raw, headers={"content-type": "text/plain"}),
        415,
    )
    _assert_problem(
        world.client.post(
            "/internal/ingest", content=raw, headers=JSON | {"content-encoding": "gzip"}
        ),
        415,
    )


# --- zstd and size caps (spec 8.5, 2.3 invariant 8) --------------------------------------------


def test_zstd_body_is_accepted(world: World) -> None:
    payload = _payload()
    compressed = zstandard.ZstdCompressor(level=3).compress(json.dumps(payload).encode())
    response = world.client.post(
        "/internal/ingest", content=compressed, headers=JSON | {"content-encoding": "zstd"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 1, "duplicate": False}
    assert len(world.writer.batches) == 1


def test_oversized_decompressed_body_is_413() -> None:
    world = _world(_settings(max_body_bytes=64 * KIB, max_decompressed_bytes=64 * KIB))
    bomb = zstandard.ZstdCompressor(level=3).compress(b"[" + b"0," * (200 * KIB))
    assert len(bomb) < 64 * KIB
    response = world.client.post(
        "/internal/ingest", content=bomb, headers=JSON | {"content-encoding": "zstd"}
    )
    _assert_problem(response, 413)
    assert world.writer.batches == []


def test_oversized_wire_body_is_413() -> None:
    world = _world(_settings(max_body_bytes=64 * KIB, max_decompressed_bytes=64 * KIB))
    response = world.client.post("/internal/ingest", content=b"0" * (70 * KIB), headers=JSON)
    _assert_problem(response, 413)


def test_malformed_zstd_is_a_400_problem(world: World) -> None:
    response = world.client.post(
        "/internal/ingest",
        content=b"\x28\xb5\x2f\xfd garbage",
        headers=JSON | {"content-encoding": "zstd"},
    )
    _assert_problem(response, 400)


# --- heartbeats --------------------------------------------------------------------------------


def test_heartbeat_is_stored(world: World) -> None:
    response = _post(world, "/internal/heartbeat", _heartbeat())
    assert response.status_code == 200, response.text
    assert response.json() == {"recorded": True}
    record = world.health.get("default", "src_wms_db")
    assert record is not None
    assert record.status == "degraded"
    assert record.lag_seconds == 12.5
    assert record.error_count == 2
    assert record.buffer_depth == 40
    assert record.oldest_buffered_at is None
    assert record.message == "splunk export slow"
    assert record.received_at == NOW
    assert "carto_heartbeats_total 1\n" in world.client.get("/metrics").text


def test_heartbeat_upsert_keeps_the_latest(world: World) -> None:
    _post(world, "/internal/heartbeat", _heartbeat())
    _post(world, "/internal/heartbeat", _heartbeat(status="ok", error_count=0))
    record = world.health.get("default", "src_wms_db")
    assert record is not None and record.status == "ok" and record.error_count == 0


def test_invalid_heartbeat_is_a_422_problem(world: World) -> None:
    _assert_problem(_post(world, "/internal/heartbeat", _heartbeat(lag_seconds=-1)), 422)
    _assert_problem(_post(world, "/internal/heartbeat", _heartbeat(tenant_id="other")), 403)
    assert world.health.get("default", "src_wms_db") is None


# --- health, readiness, metrics, request ids ---------------------------------------------------


def test_healthz_and_readyz(world: World) -> None:
    assert world.client.get("/healthz").json() == {"status": "ok"}
    ready = world.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    world.writer.ready = False
    not_ready = world.client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json() == {"status": "unavailable"}, "no detail in the body (spec 12)"


def test_metrics_are_prometheus_text(world: World) -> None:
    _post(world, "/internal/ingest", _payload([_event(0), _event(1), _event(2)]))
    _post(world, "/internal/ingest", _payload() | {"events": []})
    response = world.client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for name in (
        "carto_ingest_batches_total",
        "carto_ingest_events_total",
        "carto_ingest_duplicates_total",
        "carto_ingest_errors_total",
        "carto_heartbeats_total",
    ):
        assert f"# TYPE {name} counter\n" in text
        assert f"# HELP {name} " in text
    assert "carto_ingest_batches_total 1\n" in text
    assert "carto_ingest_events_total 3\n" in text
    assert "carto_ingest_errors_total 1\n" in text


def test_request_id_is_echoed_or_generated(world: World) -> None:
    echoed = world.client.get("/healthz", headers={"x-request-id": "edge-7f3a.42"})
    assert echoed.headers["x-request-id"] == "edge-7f3a.42"
    generated = world.client.get("/healthz")
    assert ULID_PATTERN.fullmatch(generated.headers["x-request-id"])
    replaced = world.client.get("/healthz", headers={"x-request-id": "bad id!"})
    assert ULID_PATTERN.fullmatch(replaced.headers["x-request-id"])
    too_long = world.client.get("/healthz", headers={"x-request-id": "a" * 200})
    assert ULID_PATTERN.fullmatch(too_long.headers["x-request-id"])


def test_unknown_routes_and_methods_are_problems(world: World) -> None:
    _assert_problem(world.client.get("/nope"), 404)
    _assert_problem(world.client.get("/internal/ingest"), 405)


def test_no_interactive_docs_on_the_internal_api(world: World) -> None:
    assert world.client.get("/docs").status_code == 404
    assert world.client.get("/openapi.json").status_code == 404


# --- spec 2.3 invariant 7: bodies are never logged --------------------------------------------


def test_request_bodies_and_event_contents_never_reach_the_logs() -> None:
    buffer = io.StringIO()
    configure_logging("ingest-api-test", level="debug", stream=buffer)
    world = _world()
    payload = _payload()
    payload["events"][0]["attributes"]["status"] = "LEAKCHECKVALUE"
    assert _post(world, "/internal/ingest", payload).status_code == 200
    payload["events"][0]["source_id"] = "src_other"
    assert _post(world, "/internal/ingest", payload).status_code == 422
    world.writer.fail = True
    assert _post(world, "/internal/ingest", _payload()).status_code == 503
    logs = buffer.getvalue()
    assert "batch" in logs, "something was logged"
    assert "LEAKCHECKVALUE" not in logs
    assert "DC-03" not in logs
    for token in EXAMPLE_TOKENS:
        assert token not in logs
    assert not re.search(r"\"(events|identifiers|attributes)\"\s*:\s*[\[{]", logs), (
        "a serialized body reached the logs"
    )
