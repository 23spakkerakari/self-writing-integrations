"""Splunk pull connector over the REST search export endpoint (spec 8.1.3).

- ``read(cursor)`` runs the configured search over sliding windows of ``window_minutes`` of
  **index time** (``index_earliest`` inclusive, ``index_latest`` exclusive; ADR 0035 item 2)
  from ``cursor.window_end - overlap_minutes`` (default overlap 2 minutes) up to
  ``now - settle_seconds`` (default 60), streaming the export's line-delimited JSON and
  deduplicating on ``_cd`` + ``_indextime`` + sha256(``_raw``) across windows with a bounded
  LRU. Index time only moves forward, so an event indexed late (forwarder backlog) is still
  read. ``earliest_time`` is ``window start - max_event_lag_hours`` (default 24) only to bound
  the buckets Splunk scans; an event whose own time is older than that is not read.
  After every fully searched window, empty or not, the read yields a
  :class:`~carto_edge.pipeline.model.CursorCheckpoint` ``{"window_end": <iso>}``, so a quiet
  search still moves the cursor.
- ``backfill(start, end)`` runs chunked windows of event time (default 60 minutes) with at most
  ``max_concurrency`` exports in flight (default 2) to protect the search head.
- The configured search is checked against the read-only command allowlist at construction
  (:func:`carto_edge.connectors.splunk_spl.check_search`, ADR 0037 item 4): every pipeline
  stage, subsearches included, must run a built-in read-only command.
- ``test()`` reads the token's roles (``/services/authentication/current-context``), each
  role's capabilities and ``srchIndexesAllowed`` (``/services/authorization/roles/<role>``), runs
  a one-minute ``| head 1`` search, and reports ``WRITE_CAPABLE`` when any capability is not on
  the read list (``search``, ``rtsearch``, ``list_*``, ``get_*`` and a few named read
  capabilities; ADR 0037 item 5); ``VERIFIED`` otherwise, with the indexes as ``visible``.

Every request goes through :func:`carto_edge.net.http.build_async_client`, so the host is
validated and pinned by the network policy (spec 14.7). The export endpoint is a POST by
Splunk's design and the only POST in this package (spec 8.1); it is isolated in
:meth:`SplunkConnector._export_request` with the Semgrep exception. The token is resolved at
use through the secret resolver and sent as a bearer header; it is never logged, nor are
request or response bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, field_validator

from carto_edge.config import SourceConfig
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
from carto_edge.connectors.splunk_spl import SearchCheckError, check_search, write_capabilities
from carto_edge.net.http import build_async_client
from carto_edge.net.retry import CircuitBreaker, RateLimiter, retry_async
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem
from carto_schema.event import EventKind

__all__ = ["SplunkConfig", "SplunkConnector"]

logger = logging.getLogger(__name__)

EXPORT_PATH: Final = "/services/search/jobs/export"
CONTEXT_PATH: Final = "/services/authentication/current-context"
ROLES_PATH: Final = "/services/authorization/roles"
KEPT_FIELDS: Final = frozenset({"host", "source", "sourcetype", "index", "_time"})
DROPPED_FIELDS: Final = frozenset({"_raw", "linecount", "splunk_server"})
FUTURE_TOLERANCE: Final = timedelta(days=1)
"""``latest_time`` of an index-time window: event times up to a day ahead of the window are
still read (a source clock running ahead; ADR 0036 accepts one day)."""
MAX_LINE_CHARS: Final = 4 * 1024 * 1024
MAX_JSON_BODY_BYTES: Final = 4 * 1024 * 1024
RETRY_ATTEMPTS: Final = 3


class SplunkConfig(ConnectorConfig):
    base_url: str = Field(min_length=8, max_length=512)
    search: str = Field(min_length=1, max_length=8192)
    window_minutes: int = Field(default=5, ge=1, le=1440)
    overlap_minutes: int = Field(default=2, ge=0, le=60)
    max_concurrency: int = Field(default=2, ge=1, le=8)
    backfill_chunk_minutes: int = Field(default=60, ge=1, le=1440)
    verify_tls: bool = True
    ca_file: str | None = Field(default=None, max_length=4096)
    allow_plaintext: bool = Field(default=False, description="Admin flag: allow http://.")
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    requests_per_second: float = Field(default=2.0, gt=0, le=100)
    dedupe_size: int = Field(default=100_000, ge=1000, le=10_000_000)
    max_windows_per_read: int = Field(default=288, ge=1, le=10_000)
    settle_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="A window ends no later than now minus this (ADR 0035): indexing settles.",
    )
    max_event_lag_hours: int = Field(
        default=24,
        ge=1,
        le=24 * 400,
        description="Oldest event time read, relative to the index-time window (ADR 0035).",
    )
    failure_threshold: int = Field(default=5, ge=1, le=100)
    breaker_reset_seconds: float = Field(default=60.0, gt=0, le=3600)

    @field_validator("base_url")
    @classmethod
    def _plain_origin(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            msg = "base_url must be http(s)://host[:port]"
            raise ValueError(msg)
        if "@" in parts.netloc:
            msg = "base_url must not carry credentials; use secret_ref"
            raise ValueError(msg)
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            msg = "base_url must be the origin only, without a path"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("search")
    @classmethod
    def _search_shape(cls, value: str) -> str:
        text = value.strip()
        try:
            check_search(text)
        except SearchCheckError as exc:
            raise ValueError(str(exc)) from exc
        return text


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@register("splunk")
class SplunkConnector:
    """Spec 8.1.3."""

    type: ClassVar[str] = "splunk"

    def __init__(
        self,
        source: SourceConfig,
        context: ConnectorContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.context = context
        self.config = self.validate_config(source.config)
        if source.secret_ref is None:
            msg = f"source {source.id!r}: a splunk source needs a secret_ref for its token"
            raise InvalidConfigError(msg)
        parts = urlsplit(self.config.base_url)
        if parts.scheme == "http" and not self.config.allow_plaintext:
            msg = f"source {source.id!r}: base_url uses http; set allow_plaintext to accept it"
            raise InvalidConfigError(msg)
        if parts.scheme == "http":
            logger.warning(
                "splunk source %s uses plaintext http (admin flag allow_plaintext)", source.id
            )
        self._host = parts.hostname or ""
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._transport = transport
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._client: httpx.AsyncClient | None = None
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._limiter = RateLimiter(self.config.requests_per_second, self.config.max_concurrency)
        self._breaker = CircuitBreaker(
            self.config.failure_threshold, self.config.breaker_reset_seconds
        )
        self._sequence = 0

    def validate_config(self, cfg: Mapping[str, Any]) -> SplunkConfig:
        return validate_config_model(SplunkConfig, cfg, self.source.id)

    # -- HTTP plumbing --------------------------------------------------------------------------

    def _client_or_open(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_async_client(
                self.context.network,
                ca_file=Path(self.config.ca_file) if self.config.ca_file else None,
                client_cert=None,
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=15.0),
                verify=self.config.verify_tls,
                base_url=self.config.base_url,
                transport=self._transport,
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        token_value = self.context.secrets.resolve(self.source.secret_ref or "")
        return {"Authorization": f"Bearer {token_value}"}

    def _search_text(self, suffix: str = "") -> str:
        search = self.config.search
        if not search.startswith(("search ", "|")):
            search = f"search {search}"
        return f"{search} {suffix}".strip()

    async def _get_json(self, path: str) -> dict[str, Any]:
        client = self._client_or_open()

        async def op() -> httpx.Response:
            async with self._limiter:
                return await client.get(
                    path, params={"output_mode": "json"}, headers=self._auth_headers()
                )

        response = await self._breaker.call(
            lambda: retry_async(
                op, attempts=RETRY_ATTEMPTS, base=0.5, cap=5.0, retry_on=(httpx.TransportError,)
            )
        )
        if response.status_code != 200:
            msg = f"Splunk GET {path} returned HTTP {response.status_code}"
            raise ConnectorError(msg)
        if len(response.content) > MAX_JSON_BODY_BYTES:
            msg = f"Splunk GET {path} response exceeds {MAX_JSON_BODY_BYTES} bytes"
            raise ConnectorError(msg)
        try:
            body = response.json()
        except ValueError as exc:
            msg = f"Splunk GET {path} returned a non-JSON body"
            raise ConnectorError(msg) from exc
        if not isinstance(body, dict):
            msg = f"Splunk GET {path} returned an unexpected body"
            raise ConnectorError(msg)
        return body

    async def _export_request(self, params: Mapping[str, str]) -> httpx.Response:
        """The documented search export call (spec 8.1.3). Splunk only offers it as a POST; it
        creates no object on the search head (the export job streams and disappears)."""
        client = self._client_or_open()

        async def op() -> httpx.Response:
            request = client.build_request(  # nosemgrep: carto-connector-read-only
                "POST", EXPORT_PATH, data=dict(params), headers=self._auth_headers()
            )
            return await client.send(request, stream=True)  # nosemgrep: carto-connector-read-only

        return await self._breaker.call(
            lambda: retry_async(
                op, attempts=RETRY_ATTEMPTS, base=0.5, cap=5.0, retry_on=(httpx.TransportError,)
            )
        )

    @staticmethod
    def _event_time(start: datetime, end: datetime) -> dict[str, str]:
        """Bounds on event time ``[start, end]`` (backfill and the test search)."""
        return {"earliest_time": _iso(start), "latest_time": _iso(end)}

    def _index_time(self, start: datetime, end: datetime) -> dict[str, str]:
        """Bounds on index time ``[start, end)`` (ADR 0035 item 2); event time only bounds the
        buckets scanned."""
        return {
            "index_earliest": _iso(start),
            "index_latest": _iso(end),
            "earliest_time": _iso(start - timedelta(hours=self.config.max_event_lag_hours)),
            "latest_time": _iso(end + FUTURE_TOLERANCE),
        }

    async def _export(
        self, times: Mapping[str, str], suffix: str = ""
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the ``result`` objects of one export bounded by ``times``."""
        params = {
            "search": self._search_text(suffix),
            "output_mode": "json",
            **times,
        }
        async with self._limiter:
            response = await self._export_request(params)
            try:
                if response.status_code != 200:
                    msg = f"Splunk export returned HTTP {response.status_code}"
                    raise ConnectorError(msg)
                skipped = 0
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    if len(line) > MAX_LINE_CHARS:
                        skipped += 1
                        continue
                    try:
                        parsed = json.loads(line)
                    except ValueError:
                        skipped += 1
                        continue
                    if not isinstance(parsed, dict) or parsed.get("preview") is True:
                        continue
                    result = parsed.get("result")
                    if isinstance(result, dict):
                        yield result
                if skipped:
                    logger.warning(
                        "splunk export source_id=%s skipped_lines=%d", self.source.id, skipped
                    )
            finally:
                await response.aclose()

    # -- records ----------------------------------------------------------------------------------

    def _record(self, result: Mapping[str, Any], now: datetime) -> RawRecord | None:
        raw = result.get("_raw")
        text = raw if isinstance(raw, str) else json.dumps(result, sort_keys=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cd = str(result.get("_cd", "")) or ""
        indextime = str(result.get("_indextime", "")) or "0"
        key = (cd, indextime, digest)
        if key in self._seen:
            self._seen.move_to_end(key)
            return None
        self._seen[key] = None
        while len(self._seen) > self.config.dedupe_size:
            self._seen.popitem(last=False)
        fields = {
            name: value
            for name, value in result.items()
            if name in KEPT_FIELDS or (not name.startswith("_") and name not in DROPPED_FIELDS)
        }
        locator = f"splunk:{cd}:{indextime}" if cd else f"splunk:{digest[:16]}:{indextime}"
        self._sequence += 1
        return RawRecord(
            source_id=self.source.id,
            system_id=self.source.system,
            kind=EventKind.LOG,
            locator=locator,
            received_at=now,
            text=text,
            fields=fields,
            sequence=self._sequence,
            size_bytes=len(text),
        )

    def _windows(
        self, start: datetime, end: datetime, minutes: int, limit: int
    ) -> list[tuple[datetime, datetime]]:
        windows: list[tuple[datetime, datetime]] = []
        step = timedelta(minutes=minutes)
        cursor = start
        while cursor < end and len(windows) < limit:
            window_end = min(cursor + step, end)
            windows.append((cursor, window_end))
            cursor = window_end
        return windows

    async def read(self, cursor: Cursor | None) -> AsyncIterator[ReadItem]:
        now = self._clock()
        # Whole seconds: the window bounds travel as ISO text with second precision.
        upper = (now - timedelta(seconds=self.config.settle_seconds)).replace(microsecond=0)
        overlap = timedelta(minutes=self.config.overlap_minutes)
        committed: datetime | None = None
        if cursor and cursor.get("window_end"):
            try:
                committed = _parse_iso(str(cursor["window_end"]))
            except ValueError as exc:
                msg = f"source {self.source.id!r}: cursor window_end is not an ISO timestamp"
                raise ConnectorError(msg) from exc
            start = committed - overlap
        elif self.source.backfill_days:
            start = upper - timedelta(days=self.source.backfill_days)
        else:
            start = upper - timedelta(minutes=self.config.window_minutes)
        windows: list[tuple[datetime, datetime]] = []
        if committed is None or upper > committed:  # never move the cursor backwards
            windows = self._windows(
                min(start, upper),
                upper,
                self.config.window_minutes,
                self.config.max_windows_per_read,
            )
        total = 0
        started = time.monotonic()
        for window_start, window_end in windows:
            async for result in self._export(self._index_time(window_start, window_end)):
                record = self._record(result, now)
                if record is None:
                    continue
                total += 1
                yield record
            # The window was searched to its end: commit it even when it held nothing.
            yield CursorCheckpoint(self.source.id, {"window_end": _iso(window_end)})
        logger.info(
            "splunk read source_id=%s windows=%d records=%d seconds=%.1f",
            self.source.id,
            len(windows),
            total,
            time.monotonic() - started,
        )

    async def _collect(self, start: datetime, end: datetime, now: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        async for result in self._export(self._event_time(start, end)):
            record = self._record(result, now)
            if record is not None:
                records.append(record)
        return records

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        now = self._clock()
        chunks = self._windows(start, end, self.config.backfill_chunk_minutes, 100_000)
        width = self.config.max_concurrency
        for offset in range(0, len(chunks), width):
            group = chunks[offset : offset + width]
            results = await asyncio.gather(*(self._collect(s, e, now) for s, e in group))
            for records in results:
                for record in records:
                    yield record

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- test() ---------------------------------------------------------------------------------

    @staticmethod
    def _entry_content(body: Mapping[str, Any]) -> dict[str, Any]:
        entries = body.get("entry")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            content = entries[0].get("content")
            if isinstance(content, dict):
                return content
        return {}

    @staticmethod
    def _strings(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [value]
        return []

    async def test(self) -> TestResult:
        checks: list[TestCheck] = []
        problems: list[str] = []
        capabilities: set[str] = set()
        indexes: set[str] = set()
        try:
            context = self._entry_content(await self._get_json(CONTEXT_PATH))
            roles = self._strings(context.get("roles"))
            checks.append(TestCheck("authentication", True, f"{len(roles)} roles"))
            for role in roles:
                content = self._entry_content(
                    await self._get_json(f"{ROLES_PATH}/{quote(role, safe='')}")
                )
                capabilities.update(self._strings(content.get("capabilities")))
                capabilities.update(self._strings(content.get("imported_capabilities")))
                indexes.update(self._strings(content.get("srchIndexesAllowed")))
                indexes.update(self._strings(content.get("imported_srchIndexesAllowed")))
        except (ConnectorError, httpx.HTTPError) as exc:
            checks.append(TestCheck("authentication", False, str(exc)))
            problems.append(f"Splunk authentication or role lookup failed: {exc}")
        writers = write_capabilities(capabilities)
        if writers:
            checks.append(TestCheck("capabilities", False, ", ".join(writers[:10])))
            problems.append(
                "the token's roles carry capabilities that are not on the read list "
                "(ADR 0037): " + ", ".join(writers[:10])
            )
        else:
            checks.append(TestCheck("capabilities", True, f"{len(capabilities)} read capabilities"))
        try:
            now = self._clock()
            count = 0
            window = self._event_time(now - timedelta(minutes=1), now)
            async for _result in self._export(window, "| head 1"):
                count += 1
            checks.append(TestCheck("search", True, f"{count} result"))
        except (ConnectorError, httpx.HTTPError) as exc:
            checks.append(TestCheck("search", False, str(exc)))
            problems.append(f"the one-minute test search failed: {exc}")
        read_only = ReadOnlyStatus.WRITE_CAPABLE if writers else ReadOnlyStatus.VERIFIED
        return TestResult(
            ok=not problems,
            read_only=read_only,
            checks=tuple(checks),
            problems=tuple(problems),
            visible=tuple(sorted(indexes)),
        )
