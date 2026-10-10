"""carto_edge.connectors.webhook: HMAC verification (constant time, replay window), the body
limit and records_from_body (spec 8.1.6, Appendix B)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorContext,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ResolvedHost,
)
from carto_edge.connectors.webhook import WebhookBodyError, WebhookConnector, verify_hmac_sha256
from carto_schema.event import EventKind

SECRET = "shared-secret-0123456789abcdef"  # noqa: S105
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


class Secrets:
    def __init__(self, value: str | None = SECRET) -> None:
        self.value = value

    def resolve(self, secret_ref: str) -> str:
        if self.value is None:
            raise RuntimeError("no such secret")
        return self.value


class Network:
    def resolve(self, host: str, port: int) -> ResolvedHost:
        return ResolvedHost(host=host, address="10.0.0.1", port=port)


def make(
    config: dict[str, object] | None = None, secrets: Secrets | None = None
) -> WebhookConnector:
    source = SourceConfig(
        id="src_hook",
        system="sys_orders",
        type=SourceType.WEBHOOK,
        config=config or {"event_type_field": "type"},
        secret_ref="local://hook",  # noqa: S106
    )
    return WebhookConnector(
        source, ConnectorContext(secrets=secrets or Secrets(), network=Network())
    )


def sign(body: bytes, stamp: int | None = None, secret: str = SECRET) -> str:
    signed = body if stamp is None else f"{stamp}.".encode() + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def test_is_a_read_connector() -> None:
    connector = make()
    assert isinstance(connector, ReadConnector)
    assert connector.type == "webhook"


def test_needs_a_secret_ref() -> None:
    source = SourceConfig(id="src_hook", system="sys_orders", type=SourceType.WEBHOOK)
    with pytest.raises(InvalidConfigError, match="secret_ref"):
        WebhookConnector(source, ConnectorContext(secrets=Secrets(), network=Network()))


def test_rejects_unknown_config_keys_and_oversized_limit() -> None:
    with pytest.raises(InvalidConfigError):
        make({"bogus": 1})
    with pytest.raises(InvalidConfigError):
        make({"max_body_bytes": 2 * 1024 * 1024})


# ---------------------------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------------------------


def test_valid_signature_body_only() -> None:
    body = b'{"type": "order.created"}'
    headers = {"x-carto-signature": sign(body)}
    assert make().verify_signature(SECRET, body, headers, NOW)


@pytest.mark.parametrize("prefix", ["v1=", "sha256=", ""])
def test_signature_prefixes(prefix: str) -> None:
    body = b"{}"
    assert make().verify_signature(SECRET, body, {"X-Carto-Signature": prefix + sign(body)}, NOW)


def test_multiple_candidates_any_match() -> None:
    body = b"{}"
    header = "v0=" + "0" * 64 + ", v1=" + sign(body)
    assert make().verify_signature(SECRET, body, {"X-Carto-Signature": header}, NOW)


def test_wrong_secret_or_tampered_body_is_refused() -> None:
    body = b'{"a": 1}'
    good = sign(body)
    assert not make().verify_signature("other-secret", body, {"X-Carto-Signature": good}, NOW)
    assert not make().verify_signature(SECRET, b'{"a": 2}', {"X-Carto-Signature": good}, NOW)


def test_unsigned_request_is_refused() -> None:
    assert not make().verify_signature(SECRET, b"{}", {}, NOW)
    assert not make().verify_signature(SECRET, b"{}", {"X-Carto-Signature": ""}, NOW)
    assert not make().verify_signature(SECRET, b"{}", {"X-Carto-Signature": "v1="}, NOW)


def test_timestamp_inside_window_signs_timestamp_dot_body() -> None:
    body = b'{"a": 1}'
    stamp = int(NOW.timestamp()) - 200
    headers = {"X-Carto-Timestamp": str(stamp), "X-Carto-Signature": "v1=" + sign(body, stamp)}
    assert make().verify_signature(SECRET, body, headers, NOW)
    # The body-only signature is not accepted once a timestamp is presented.
    headers["X-Carto-Signature"] = "v1=" + sign(body)
    assert not make().verify_signature(SECRET, body, headers, NOW)


@pytest.mark.parametrize("offset", [301, -301, 100_000])
def test_timestamp_outside_window_is_a_replay(offset: int) -> None:
    body = b"{}"
    stamp = int(NOW.timestamp()) - offset
    headers = {"X-Carto-Timestamp": str(stamp), "X-Carto-Signature": sign(body, stamp)}
    assert not make().verify_signature(SECRET, body, headers, NOW)


@pytest.mark.parametrize("stamp", ["", "abc", "1e9", "-5", "9" * 20])
def test_malformed_timestamp_is_refused(stamp: str) -> None:
    body = b"{}"
    headers = {"X-Carto-Timestamp": stamp, "X-Carto-Signature": sign(body)}
    assert not make().verify_signature(SECRET, body, headers, NOW)


def test_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def recording(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", recording)
    body = b"{}"
    header = "v1=" + sign(body) + ", v2=" + "f" * 64
    assert make().verify_signature(SECRET, body, {"X-Carto-Signature": header}, NOW)
    # Both candidates were compared with compare_digest; no early exit on the first match.
    assert len(calls) == 2


def test_module_function_matches_method() -> None:
    body = b"[]"
    assert verify_hmac_sha256(
        SECRET,
        body,
        {"X-Carto-Signature": sign(body)},
        NOW,
        signature_header="X-Carto-Signature",
        timestamp_header=None,
        window_seconds=300,
    )


# ---------------------------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------------------------


def test_single_object_body() -> None:
    body = b'{"type": "order.created", "order_id": "SO-1"}'
    records = make().records_from_body(body, NOW)
    assert len(records) == 1
    record = records[0]
    digest = hashlib.sha256(body).hexdigest()[:16]
    assert record.locator == f"webhook:{digest}:0"
    assert record.kind is EventKind.WEBHOOK
    assert record.fields == {"type": "order.created", "order_id": "SO-1"}
    assert record.template_hint == "order.created"
    assert record.received_at == NOW
    assert record.size_bytes == len(body)
    assert record.source_id == "src_hook"
    assert record.system_id == "sys_orders"
    assert record.commit_cursor is None


def test_array_body_one_record_per_object() -> None:
    body = json.dumps([{"type": "a", "n": 1}, {"type": "b", "n": 2}, {"n": 3}]).encode()
    records = make().records_from_body(body, NOW)
    assert [record.locator.rsplit(":", 1)[1] for record in records] == ["0", "1", "2"]
    assert [record.template_hint for record in records] == ["a", "b", None]
    assert [record.sequence for record in records] == [0, 1, 2]


def test_event_type_must_be_scalar() -> None:
    records = make().records_from_body(b'{"type": {"nested": 1}}', NOW)
    assert records[0].template_hint is None
    records = make().records_from_body(b'{"type": 7}', NOW)
    assert records[0].template_hint == "7"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        b'"text"',
        b"42",
        b"[{}, 3]",
        b"",
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="bracket-bomb"),
    ],
)
def test_bad_bodies(body: bytes) -> None:
    with pytest.raises(WebhookBodyError):
        make().records_from_body(body, NOW)


def test_body_limit() -> None:
    connector = make({"max_body_bytes": 1024})
    body = b'{"pad": "' + b"x" * 1100 + b'"}'
    with pytest.raises(WebhookBodyError, match="exceeds"):
        connector.records_from_body(body, NOW)


def test_depth_limit() -> None:
    connector = make({"max_json_depth": 4})
    assert connector.records_from_body(b'{"a": {"b": {"c": 1}}}', NOW)
    with pytest.raises(WebhookBodyError, match="deeper"):
        connector.records_from_body(b'{"a": {"b": {"c": {"d": 1}}}}', NOW)


def test_record_count_limit() -> None:
    connector = make({"max_records": 2})
    with pytest.raises(WebhookBodyError, match="more than"):
        connector.records_from_body(b"[{}, {}, {}]", NOW)


# ---------------------------------------------------------------------------------------------
# connector interface
# ---------------------------------------------------------------------------------------------


async def test_read_and_backfill_yield_nothing() -> None:
    connector = make()
    assert [record async for record in connector.read(None)] == []
    assert [record async for record in connector.backfill(NOW, NOW)] == []
    await connector.close()


async def test_test_checks_the_secret() -> None:
    result = await make().test()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.can_enable
    bad = await make(secrets=Secrets(None)).test()
    assert not bad.ok
    assert "resolved" in bad.problems[0]
    short = await make(secrets=Secrets("short")).test()
    assert not short.ok


@pytest.mark.parametrize("header", ["v1=\u00e9", "sha256=" + "\u0661" * 64, "v1=zz", "v1=abc"])
def test_malformed_signature_candidates_are_refused_without_raising(header: str) -> None:
    connector = make()
    body = b'{"a": 1}'
    assert not connector.verify_signature(SECRET, body, {"X-Carto-Signature": header}, NOW)


def test_a_unicode_digit_timestamp_is_refused_without_raising() -> None:
    connector = make()
    body = b'{"a": 1}'
    headers = {"X-Carto-Signature": sign(body), "X-Carto-Timestamp": "\u00b2"}
    assert not connector.verify_signature(SECRET, body, headers, NOW)
