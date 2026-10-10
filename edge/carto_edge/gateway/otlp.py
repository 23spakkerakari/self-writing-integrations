"""OTLP/HTTP logs receiver: ``ExportLogsServiceRequest`` bytes to raw records (spec 8.1.2).

The collector (``otel/collector.yaml``) exports with ``otlphttp``: protobuf, ``gzip`` or
``zstd``, and every receiver stamps the ``carto.source_id`` resource attribute with the id of
an ``otlp`` source in the sources file. :func:`parse_logs_request` turns one request into
:class:`~carto_edge.pipeline.model.RawRecord` objects grouped by that id:

- The body is decompressed with the output capped at ``max_bytes`` (spec 2.3 invariant 8: no
  zip bombs) and parsed as protobuf. Failures raise :class:`OtlpBodyError` with a reason code
  (``too_large``, ``bad_encoding``, ``bad_protobuf``) and a fixed message that never quotes the
  body.
- A resource without a string ``carto.source_id``, or with one the sources file does not
  configure as an enabled ``otlp`` source, has its log records counted as rejected (the
  response's ``partial_success``). At most :data:`MAX_LOG_RECORDS` records per request are
  accepted; the rest are rejected too.
- A string body becomes ``text`` (a log line: the parser picks its format). A key-value body
  becomes ``fields``, converted to JSON-like Python with nesting capped at
  :data:`MAX_VALUE_DEPTH` and arrays at :data:`MAX_ARRAY_ITEMS`. Log record attributes other
  than ``log.file.*`` (the file name is a locator detail, not record content) are merged into
  ``fields`` without overwriting body keys. Any other body type lands under ``body``.
- When the record has fields, the OTLP ``severity_text`` (else a non-zero ``severity_number``)
  and the event time (``time_unix_nano``) are added under the keys the parser reads
  (``severity_text``, ``severity_number``, ``timestamp``) unless the record already carries
  such a key. A bare line gets neither, so it keeps the parser's format detection (adding a
  field would turn the whole line into a message to mine).
- ``locator`` is ``otlp:`` plus the first 16 hex characters of
  ``sha256(source_id 0x00 body 0x00 time)`` where ``time`` is ``time_unix_nano``, else
  ``observed_time_unix_nano`` (the collector's read time, kept in its persistent queue): the
  same record retried by the collector gets the same locator, and so the same ``event_id``.

Nothing here logs; the caller logs counts.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

import zstandard
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

from carto_edge.config import DEFAULT_TIMESTAMP_FIELDS
from carto_edge.pipeline.model import RawRecord
from carto_edge.pipeline.severity import LEVEL_FIELDS, OTEL_NUMBER_FIELDS
from carto_schema.event import EventKind

__all__ = [
    "IGNORED_ATTRIBUTE_PREFIX",
    "LOCATOR_DIGEST_LEN",
    "MAX_ARRAY_ITEMS",
    "MAX_LOG_RECORDS",
    "MAX_VALUE_DEPTH",
    "OTLP_ENCODINGS",
    "SOURCE_ID_ATTRIBUTE",
    "OtlpBodyError",
    "OtlpRecords",
    "Reason",
    "parse_logs_request",
]

SOURCE_ID_ATTRIBUTE: Final = "carto.source_id"
IGNORED_ATTRIBUTE_PREFIX: Final = "log.file."
MAX_LOG_RECORDS: Final = 10_000
MAX_VALUE_DEPTH: Final = 12
MAX_ARRAY_ITEMS: Final = 20
LOCATOR_DIGEST_LEN: Final = 16
OTLP_ENCODINGS: Final[frozenset[str]] = frozenset({"", "identity", "gzip", "zstd"})
"""``Content-Encoding`` values accepted (lower-cased; empty means none)."""

SEVERITY_TEXT_FIELD: Final = "severity_text"
SEVERITY_NUMBER_FIELD: Final = "severity_number"
TIMESTAMP_FIELD: Final = "timestamp"
BODY_FIELD: Final = "body"

_SEVERITY_KEYS: Final = frozenset((*LEVEL_FIELDS, *OTEL_NUMBER_FIELDS))
_TIMESTAMP_KEYS: Final = frozenset(DEFAULT_TIMESTAMP_FIELDS)
_DECOMPRESS_CHUNK: Final = 64 * 1024
_NANOS: Final = 1_000_000_000
_MAX_UNIX_SECONDS: Final = 253_402_300_799  # 9999-12-31T23:59:59Z

Reason = Literal["too_large", "bad_encoding", "bad_protobuf"]


class OtlpBodyError(ValueError):
    """The request body cannot be read. ``reason`` is a fixed code; the message never quotes
    the body."""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason: Reason = reason


@dataclass(slots=True)
class OtlpRecords:
    """One request's records by source id, with the counts the OTLP response reports."""

    records: dict[str, list[RawRecord]] = field(default_factory=dict)
    accepted: int = 0
    rejected: int = 0

    def all_records(self) -> list[RawRecord]:
        return [record for records in self.records.values() for record in records]


# ---------------------------------------------------------------------------------------------
# Decompression
# ---------------------------------------------------------------------------------------------


def _too_large(limit: int) -> OtlpBodyError:
    return OtlpBodyError("too_large", f"decompressed body exceeds {limit} bytes")


def _gunzip_capped(data: bytes, limit: int) -> bytes:
    """Every gzip member of ``data``, inflated with the output capped at ``limit`` bytes."""
    out = bytearray()
    pending = data
    try:
        while pending:
            inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            out += inflater.decompress(pending, limit + 1 - len(out))
            if len(out) > limit:
                raise _too_large(limit)
            if not inflater.eof:
                msg = "gzip body is truncated"
                raise OtlpBodyError("bad_encoding", msg)
            pending = inflater.unused_data
    except zlib.error as exc:
        msg = "gzip body is malformed"
        raise OtlpBodyError("bad_encoding", msg) from exc
    return bytes(out)


def _unzstd_capped(data: bytes, limit: int) -> bytes:
    """Every zstd frame of ``data``, read in chunks with the output capped at ``limit``."""
    out = bytearray()
    try:
        decompressor = zstandard.ZstdDecompressor()
        with decompressor.stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
            while True:
                chunk = reader.read(_DECOMPRESS_CHUNK)
                if not chunk:
                    break
                out += chunk
                if len(out) > limit:
                    raise _too_large(limit)
    except zstandard.ZstdError as exc:
        msg = "zstd body is malformed"
        raise OtlpBodyError("bad_encoding", msg) from exc
    return bytes(out)


def _decode(body: bytes, content_encoding: str, max_bytes: int) -> bytes:
    encoding = content_encoding.strip().lower()
    if encoding not in OTLP_ENCODINGS:
        msg = "unsupported content encoding; send gzip, zstd or none"
        raise OtlpBodyError("bad_encoding", msg)
    if encoding == "gzip":
        return _gunzip_capped(body, max_bytes)
    if encoding == "zstd":
        return _unzstd_capped(body, max_bytes)
    if len(body) > max_bytes:
        raise _too_large(max_bytes)
    return body


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------


def _kvlist(values: common_pb2.KeyValueList, depth: int) -> dict[str, Any]:
    """A key-value list as a dict; ``depth`` is the nesting level of the dict itself."""
    out: dict[str, Any] = {}
    for pair in values.values:
        if pair.key and pair.key not in out:
            out[pair.key] = _any_value(pair.value, depth + 1)
    return out


def _any_value(value: common_pb2.AnyValue, depth: int) -> Any:
    """JSON-like Python for an ``AnyValue`` found at nesting level ``depth``. Containers
    deeper than :data:`MAX_VALUE_DEPTH` become ``None``; arrays keep their first
    :data:`MAX_ARRAY_ITEMS` elements; bytes become base64 text; NaN and infinities ``None``."""
    kind = value.WhichOneof("value")
    result: Any = None
    if kind == "string_value":
        result = value.string_value
    elif kind == "bool_value":
        result = value.bool_value
    elif kind == "int_value":
        result = value.int_value
    elif kind == "double_value":
        number = value.double_value
        result = number if math.isfinite(number) else None
    elif kind == "bytes_value":
        result = base64.b64encode(value.bytes_value).decode("ascii")
    elif depth > MAX_VALUE_DEPTH:
        result = None
    elif kind == "array_value":
        items = value.array_value.values[:MAX_ARRAY_ITEMS]
        result = [_any_value(item, depth + 1) for item in items]
    elif kind == "kvlist_value":
        result = _kvlist(value.kvlist_value, depth)
    return result


def _source_id(resource: resource_pb2.Resource) -> str | None:
    for pair in resource.attributes:
        if pair.key == SOURCE_ID_ATTRIBUTE:
            if pair.value.WhichOneof("value") == "string_value" and pair.value.string_value:
                return pair.value.string_value
            return None
    return None


def _iso_from_unix_nano(nanos: int) -> str | None:
    seconds, rest = divmod(nanos, _NANOS)
    if not 0 < seconds <= _MAX_UNIX_SECONDS:
        return None
    moment = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=rest // 1000)
    return moment.isoformat().replace("+00:00", "Z")


def _locator(source_id: str, body: bytes, nanos: int) -> str:
    digest = hashlib.sha256()
    digest.update(source_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(body)
    digest.update(b"\x00")
    digest.update(str(nanos).encode("ascii"))
    return f"otlp:{digest.hexdigest()[:LOCATOR_DIGEST_LEN]}"


def _record(
    log_record: logs_pb2.LogRecord,
    *,
    source_id: str,
    system_id: str,
    received_at: datetime,
    sequence: int,
) -> RawRecord:
    body = log_record.body
    body_bytes: bytes = body.SerializeToString(deterministic=True)
    kind = body.WhichOneof("value")
    text: str | None = None
    fields: dict[str, Any] = {}
    if kind == "string_value":
        text = body.string_value
    elif kind == "kvlist_value":
        fields = _kvlist(body.kvlist_value, 1)
    elif kind is not None:
        fields[BODY_FIELD] = _any_value(body, 1)
    for pair in log_record.attributes:
        key = pair.key
        if key and not key.startswith(IGNORED_ATTRIBUTE_PREFIX) and key not in fields:
            fields[key] = _any_value(pair.value, 1)
    if fields:
        if _SEVERITY_KEYS.isdisjoint(fields):
            if log_record.severity_text:
                fields[SEVERITY_TEXT_FIELD] = log_record.severity_text
            elif log_record.severity_number:
                fields[SEVERITY_NUMBER_FIELD] = int(log_record.severity_number)
        if _TIMESTAMP_KEYS.isdisjoint(fields):
            stamp = _iso_from_unix_nano(log_record.time_unix_nano)
            if stamp is not None:
                fields[TIMESTAMP_FIELD] = stamp
    nanos = log_record.time_unix_nano or log_record.observed_time_unix_nano
    return RawRecord(
        source_id=source_id,
        system_id=system_id,
        kind=EventKind.LOG,
        locator=_locator(source_id, body_bytes, nanos),
        received_at=received_at,
        text=text,
        fields=fields or None,
        sequence=sequence,
        size_bytes=len(body_bytes),
        template_hint=None,
    )


# ---------------------------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------------------------


def parse_logs_request(
    body: bytes,
    *,
    content_encoding: str,
    max_bytes: int,
    received_at: datetime,
    system_of: Mapping[str, str],
) -> OtlpRecords:
    """Decode one ``ExportLogsServiceRequest`` (see the module docstring). ``system_of`` maps
    the enabled ``otlp`` source ids to their system ids; ``sequence`` is the record's position
    in the request."""
    data = _decode(body, content_encoding, max_bytes)
    request = logs_service_pb2.ExportLogsServiceRequest()
    try:
        request.ParseFromString(data)
    except (DecodeError, RecursionError) as exc:
        msg = "body is not an OTLP ExportLogsServiceRequest"
        raise OtlpBodyError("bad_protobuf", msg) from exc
    del data
    result = OtlpRecords()
    position = 0
    for resource_logs in request.resource_logs:
        source_id = _source_id(resource_logs.resource)
        system_id = system_of.get(source_id) if source_id is not None else None
        for scope_logs in resource_logs.scope_logs:
            log_records = scope_logs.log_records
            count = len(log_records)
            if source_id is None or system_id is None:
                result.rejected += count
                position += count
                continue
            take = min(count, MAX_LOG_RECORDS - result.accepted)
            if take > 0:
                bucket = result.records.setdefault(source_id, [])
                for offset in range(take):
                    bucket.append(
                        _record(
                            log_records[offset],
                            source_id=source_id,
                            system_id=system_id,
                            received_at=received_at,
                            sequence=position + offset,
                        )
                    )
                result.accepted += take
            result.rejected += count - take
            position += count
    return result
