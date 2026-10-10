"""Webhook receiver connector (spec 8.1.6): signature verification and body to records.

The source pushes; nothing is polled, so ``read`` and ``backfill`` yield nothing and ``test``
only checks that the shared secret resolves. The FastAPI route (``gateway/webhook.py``) reads
the body under the size limit, calls :meth:`WebhookConnector.verify_signature` and, when it
returns True, :meth:`WebhookConnector.records_from_body`.

Signature scheme ``hmac-sha256``: the header carries the hex HMAC-SHA256 of the raw body under
the shared secret, as ``v1=<hex>`` (Appendix B), ``sha256=<hex>`` or bare hex; several
comma-separated values are tried. When the timestamp header is present its value (epoch
seconds) must be within the window and the signed text is ``<timestamp>.<body>`` (Appendix B),
which defeats replay. Comparison is constant-time. Unsigned requests are rejected (spec 8.1.6).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Final, Literal

from pydantic import Field

from carto_edge.config import MIB, SourceConfig
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorError,
    Cursor,
    InvalidConfigError,
    ReadOnlyStatus,
    TestCheck,
    TestResult,
)
from carto_edge.connectors.registry import register, validate_config_model
from carto_edge.pipeline.model import RawRecord
from carto_schema.event import EventKind

__all__ = ["WebhookBodyError", "WebhookConfig", "WebhookConnector", "verify_hmac_sha256"]

logger = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES: Final = MIB
"""Spec 8.1.6: body size limit 1 MB."""

LOCATOR_DIGEST_LEN: Final = 16
_HEX_DIGEST_LEN: Final = 64
_HEX: Final = frozenset("0123456789abcdef")
_NO_RECORDS: Final[tuple[RawRecord, ...]] = ()


class WebhookBodyError(ConnectorError):
    """The body is too large, not JSON, too deep or not an object / array of objects."""


class WebhookConfig(ConnectorConfig):
    signature_header: str = Field(default="X-Carto-Signature", min_length=1, max_length=128)
    signature_scheme: Literal["hmac-sha256"] = "hmac-sha256"
    timestamp_header: str | None = Field(default="X-Carto-Timestamp", max_length=128)
    timestamp_window_seconds: int = Field(default=300, ge=1, le=3600)
    event_type_field: str | None = Field(default=None, max_length=256)
    max_body_bytes: int = Field(default=MAX_WEBHOOK_BODY_BYTES, ge=1024, le=MAX_WEBHOOK_BODY_BYTES)
    max_json_depth: int = Field(default=32, ge=1, le=256)
    max_records: int = Field(default=1000, ge=1, le=10_000)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _candidates(header_value: str) -> list[str]:
    found: list[str] = []
    for piece in header_value.split(","):
        text = piece.strip()
        if "=" in text:
            _label, _, text = text.partition("=")
        text = text.strip().lower()
        if len(text) == _HEX_DIGEST_LEN and text.isascii() and all(c in _HEX for c in text):
            found.append(text)
    return found


def verify_hmac_sha256(
    secret: str,
    body: bytes,
    headers: Mapping[str, str],
    now: datetime,
    *,
    signature_header: str,
    timestamp_header: str | None,
    window_seconds: int,
) -> bool:
    """True when a signature header carries HMAC-SHA256(secret, [timestamp "."] body)."""
    provided = _header(headers, signature_header)
    if not provided:
        return False
    signed = body
    if timestamp_header is not None:
        stamp = _header(headers, timestamp_header)
        if stamp is not None:
            stamp = stamp.strip()
            if not (stamp.isascii() and stamp.isdigit()) or len(stamp) > 12:
                return False
            if abs(int(now.timestamp()) - int(stamp)) > window_seconds:
                return False
            signed = stamp.encode("ascii") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    accepted = False
    for candidate in _candidates(provided):
        # Every candidate is compared so the loop's timing does not depend on which one matches.
        if hmac.compare_digest(expected, candidate):
            accepted = True
    return accepted


def _json_depth(value: object, limit: int) -> int:
    """Nesting depth of a decoded JSON value, iteratively; stops counting past ``limit``."""
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        deepest = max(deepest, depth)
        if deepest > limit:
            return deepest
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return deepest


@register("webhook")
class WebhookConnector:
    """Spec 8.1.6. Push only: the gateway route feeds :meth:`records_from_body`."""

    type: ClassVar[str] = "webhook"

    def __init__(self, source: SourceConfig, context: ConnectorContext) -> None:
        self.source = source
        self.context = context
        self.config = self.validate_config(source.config)
        if source.secret_ref is None:
            msg = f"source {source.id!r}: a webhook source needs a secret_ref for its HMAC secret"
            raise InvalidConfigError(msg)

    def validate_config(self, cfg: Mapping[str, Any]) -> WebhookConfig:
        return validate_config_model(WebhookConfig, cfg, self.source.id)

    async def test(self) -> TestResult:
        checks: list[TestCheck] = []
        problems: list[str] = []
        try:
            secret = self.context.secrets.resolve(self.source.secret_ref or "")
        except Exception as exc:  # any resolver failure is a test problem
            checks.append(TestCheck("secret", False, type(exc).__name__))
            problems.append(f"the HMAC secret could not be resolved: {exc}")
        else:
            ok = len(secret) >= 16
            checks.append(TestCheck("secret", ok, "resolved" if ok else "shorter than 16 bytes"))
            if not ok:
                problems.append("the HMAC secret must be at least 16 characters")
        checks.append(TestCheck("read_only", True, "push only: the edge never calls the source"))
        return TestResult(
            ok=not problems,
            read_only=ReadOnlyStatus.VERIFIED,
            checks=tuple(checks),
            problems=tuple(problems),
        )

    async def read(self, cursor: Cursor | None) -> AsyncIterator[RawRecord]:
        _ = cursor
        for record in _NO_RECORDS:  # an async generator that yields nothing
            yield record

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        _ = (start, end)
        for record in _NO_RECORDS:
            yield record

    async def close(self) -> None:
        return None

    # -- what the gateway route calls -----------------------------------------------------------

    def verify_signature(
        self, secret: str, body: bytes, headers: Mapping[str, str], now: datetime | None = None
    ) -> bool:
        return verify_hmac_sha256(
            secret,
            body,
            headers,
            now if now is not None else datetime.now(UTC),
            signature_header=self.config.signature_header,
            timestamp_header=self.config.timestamp_header,
            window_seconds=self.config.timestamp_window_seconds,
        )

    def records_from_body(self, body: bytes, received_at: datetime) -> list[RawRecord]:
        """One record per JSON object in the body (an object, or an array of objects)."""
        if len(body) > self.config.max_body_bytes:
            msg = f"webhook body exceeds {self.config.max_body_bytes} bytes"
            raise WebhookBodyError(msg)
        try:
            decoded = json.loads(body)
        except (ValueError, RecursionError) as exc:
            msg = "webhook body is not valid JSON"
            raise WebhookBodyError(msg) from exc
        if _json_depth(decoded, self.config.max_json_depth) > self.config.max_json_depth:
            msg = f"webhook body nests deeper than {self.config.max_json_depth}"
            raise WebhookBodyError(msg)
        if isinstance(decoded, dict):
            objects: list[dict[str, Any]] = [decoded]
        elif isinstance(decoded, list):
            if len(decoded) > self.config.max_records:
                msg = f"webhook body carries more than {self.config.max_records} records"
                raise WebhookBodyError(msg)
            if not all(isinstance(item, dict) for item in decoded):
                msg = "webhook body array must contain only objects"
                raise WebhookBodyError(msg)
            objects = decoded
        else:
            msg = "webhook body must be a JSON object or an array of objects"
            raise WebhookBodyError(msg)
        digest = hashlib.sha256(body).hexdigest()[:LOCATOR_DIGEST_LEN]
        single = len(objects) == 1
        records: list[RawRecord] = []
        for index, obj in enumerate(objects):
            records.append(
                RawRecord(
                    source_id=self.source.id,
                    system_id=self.source.system,
                    kind=EventKind.WEBHOOK,
                    locator=f"webhook:{digest}:{index}",
                    received_at=received_at,
                    fields=obj,
                    sequence=index,
                    size_bytes=len(body) if single else len(json.dumps(obj)),
                    template_hint=self._template_hint(obj),
                )
            )
        logger.debug("webhook body accepted source_id=%s records=%d", self.source.id, len(records))
        return records

    def _template_hint(self, obj: Mapping[str, Any]) -> str | None:
        field_name = self.config.event_type_field
        if field_name is None:
            return None
        value = obj.get(field_name)
        if isinstance(value, bool) or not isinstance(value, str | int):
            return None
        text = str(value).strip()
        return text[:128] if text else None
