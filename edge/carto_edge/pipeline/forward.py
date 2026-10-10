"""Forwarding to core: the mutual-TLS client, the forwarder, source health and heartbeats
(spec 8.1 common requirements, 8.5, 12 "Internal", 14.4, 16; plan M1 wave 2, D1).

- :func:`build_core_client` is the one ``httpx.Client`` the edge uses toward ``ingest-api``:
  ``base_url`` from ``core.url`` (https only, checked by the settings), TLS 1.2 minimum with the
  install CA and the edge's client certificate (:func:`carto_edge.net.http.build_ssl_context`,
  spec 14.4 internal mutual TLS), ``trust_env=False`` so no proxy or certificate setting leaks
  in from the environment, no redirects, and the ``core.timeout_seconds`` timeout.
- :class:`Forwarder` drains the :class:`~carto_edge.pipeline.buffer.DiskBuffer` oldest first:
  ``POST /internal/ingest`` with the stored zstd payload as is (``Content-Encoding: zstd``) and
  ``X-Request-Id`` set to the batch id, so a retried batch is the same request and core's batch
  ledger acknowledges it as a duplicate (ADR 0015). 2xx acknowledges the row. 400, 403, 413,
  415 and 422 mean core will never take this batch: the row is parked (kept, counted, out of
  the queue) so one bad batch cannot stall the source. 429 honours ``Retry-After`` (seconds,
  capped); 5xx, any other status and transport errors back off exponentially with jitter
  (:func:`carto_edge.net.retry.backoff_delay`) up to ``retry_max_seconds``, and the row stays.
- :class:`SourceHealth` keeps, per source, ``last_success_at``, ``lag_seconds``, the
  consecutive error count and the records read (spec 8.1 "Health"); the status is ``ok`` after a
  successful read, ``degraded`` after one or two consecutive errors and ``failing`` from three.
- :class:`Heartbeater` posts one :class:`~carto_schema.ingest.SourceHeartbeat` per enabled
  source to ``POST /internal/heartbeat``, so core's detector can tell "no data" from "nothing
  happened" (spec 8.1). The heartbeat's ``message`` is always empty: free text from a connector
  error could carry a value.

Nothing here logs a request or response body (spec 2.3 invariant 7, 14.12): log lines carry
batch ids, source ids, event counts, status codes and exception type names.
"""

from __future__ import annotations

import math
import random
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_edge.config import CoreLinkSettings, SourcesFile
from carto_edge.metrics import EdgeMetrics
from carto_edge.net.http import USER_AGENT, build_ssl_context
from carto_edge.net.retry import backoff_delay
from carto_edge.pipeline.buffer import BufferedBatch, DiskBuffer
from carto_schema.event import SCHEMA_VERSION
from carto_schema.ingest import SourceHeartbeat, SourceStatus

__all__ = [
    "BACKOFF_BASE_SECONDS",
    "FAILING_AFTER_ERRORS",
    "HEARTBEAT_PATH",
    "IDLE_WAIT_SECONDS",
    "INGEST_PATH",
    "MAX_BACKOFF_ATTEMPT",
    "MIN_RETRY_WAIT_SECONDS",
    "PARK_STATUSES",
    "Forwarder",
    "Heartbeater",
    "SendReport",
    "SourceHealth",
    "SourceHealthState",
    "build_core_client",
]

INGEST_PATH: Final = "/internal/ingest"
HEARTBEAT_PATH: Final = "/internal/heartbeat"
PARK_STATUSES: Final = frozenset({400, 403, 413, 415, 422})
"""Statuses after which core will never accept the batch as sent (spec 12, RFC 7807 problems)."""
IDLE_WAIT_SECONDS: Final = 0.5
BACKOFF_BASE_SECONDS: Final = 0.5
FAILING_AFTER_ERRORS: Final = 3
MIN_RETRY_WAIT_SECONDS: Final = 0.05
MAX_BACKOFF_ATTEMPT: Final = 32
_JSON: Final = "application/json"

Clock = Callable[[], datetime]

log = get_logger(component="carto_edge.forward")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_core_client(
    link: CoreLinkSettings, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """The client toward ``ingest-api`` (module docstring). ``transport`` is for tests
    (``httpx.MockTransport``); production leaves it unset so the mutual-TLS context is used."""
    if link.url is None:
        msg = "core.url is not set; the edge cannot forward to ingest-api without it"
        raise ValueError(msg)
    if (link.cert_file is None) != (link.key_file is None):
        msg = "core.cert_file and core.key_file must be set together (spec 14.4 mutual TLS)"
        raise ValueError(msg)
    client_cert = (
        (link.cert_file, link.key_file)
        if link.cert_file is not None and link.key_file is not None
        else None
    )
    verify: ssl.SSLContext | bool = True
    if transport is None:
        if client_cert is None:
            log.warning("core_client.no_client_certificate", url=link.url)
        verify = build_ssl_context(ca_file=link.ca_file, client_cert=client_cert, verify=True)
    return httpx.Client(
        base_url=link.url,
        verify=verify,
        transport=transport,
        timeout=httpx.Timeout(link.timeout_seconds),
        trust_env=False,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )


# ---------------------------------------------------------------------------------------------
# Forwarder
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SendReport:
    """What one :meth:`Forwarder.send_once` did. ``retry_after_seconds`` is set on failures:
    how long the caller should wait before the next attempt."""

    idle: bool = False
    sent: bool = False
    parked: bool = False
    failed: bool = False
    status: int | None = None
    retry_after_seconds: float | None = None
    batch_id: str | None = None


def _retry_after(response: httpx.Response, cap: float) -> float | None:
    """``Retry-After`` as delay seconds, capped; ``None`` when absent or not a number (the
    HTTP-date form is not used by ``ingest-api``)."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds):
        return None
    return min(cap, max(0.0, seconds))


class Forwarder:
    """Sends buffered batches to ``ingest-api`` one at a time, oldest first (spec 8.5)."""

    __slots__ = (
        "_attempt",
        "_buffer",
        "_client",
        "_clock",
        "_metrics",
        "_retry_max",
        "_rng",
        "_sleep",
    )

    def __init__(
        self,
        buffer: DiskBuffer,
        client: httpx.Client,
        *,
        metrics: EdgeMetrics,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_max_seconds: float = 60.0,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if retry_max_seconds < BACKOFF_BASE_SECONDS:
            msg = f"retry_max_seconds must be at least {BACKOFF_BASE_SECONDS}"
            raise ValueError(msg)
        self._buffer = buffer
        self._client = client
        self._metrics = metrics
        self._clock = clock if clock is not None else _utc_now
        self._sleep = sleep
        self._retry_max = retry_max_seconds
        self._rng = rng
        self._attempt = 0

    def __repr__(self) -> str:
        return f"Forwarder(retry_max_seconds={self._retry_max})"

    def _backoff(self) -> float:
        # Bounded so 2**attempt cannot overflow during a long core outage; the cap rules anyway.
        self._attempt = min(self._attempt + 1, MAX_BACKOFF_ATTEMPT)
        return backoff_delay(
            self._attempt, base=BACKOFF_BASE_SECONDS, cap=self._retry_max, rng=self._rng
        )

    def send_once(self) -> SendReport:
        """Send the oldest pending batch, if any, and act on the answer (module docstring)."""
        row = self._buffer.oldest()
        if row is None:
            return SendReport(idle=True)
        try:
            response = self._client.post(
                INGEST_PATH,
                content=row.payload,
                headers={
                    "Content-Type": _JSON,
                    "Content-Encoding": "zstd",
                    "X-Request-Id": row.batch_id,
                },
            )
        except httpx.HTTPError as exc:
            self._metrics.inc("carto_edge_send_errors_total")
            delay = self._backoff()
            log.warning(
                "forward.transport_error",
                error=type(exc).__name__,
                batch_id=row.batch_id,
                source_id=row.source_id,
                retry_in_seconds=round(delay, 3),
            )
            return SendReport(failed=True, retry_after_seconds=delay, batch_id=row.batch_id)
        return self._answer(row, response.status_code, response)

    def _answer(self, row: BufferedBatch, status: int, response: httpx.Response) -> SendReport:
        context = {
            "status": status,
            "batch_id": row.batch_id,
            "source_id": row.source_id,
            "events": row.events,
        }
        if 200 <= status < 300:
            self._buffer.ack([row.id])
            self._metrics.inc("carto_edge_batches_sent_total")
            self._attempt = 0
            log.debug("forward.sent", **context)
            return SendReport(sent=True, status=status, batch_id=row.batch_id)
        if status in PARK_STATUSES:
            self._buffer.park(row.id)
            self._metrics.inc("carto_edge_batches_parked_total")
            self._attempt = 0
            log.warning("forward.parked", **context)
            return SendReport(parked=True, status=status, batch_id=row.batch_id)
        if status == 429:
            honoured = _retry_after(response, self._retry_max)
            delay = self._backoff() if honoured is None else honoured
            if honoured is not None:
                self._attempt = min(self._attempt + 1, MAX_BACKOFF_ATTEMPT)
            log.info("forward.throttled", retry_in_seconds=round(delay, 3), **context)
            return SendReport(
                failed=True, status=status, retry_after_seconds=delay, batch_id=row.batch_id
            )
        self._metrics.inc("carto_edge_send_errors_total")
        delay = self._backoff()
        log.warning("forward.refused", retry_in_seconds=round(delay, 3), **context)
        return SendReport(
            failed=True, status=status, retry_after_seconds=delay, batch_id=row.batch_id
        )

    def refresh_gauges(self) -> None:
        """Buffer depth, bytes and the age of the oldest pending batch (spec 8.5, 16)."""
        self._metrics.set("carto_edge_buffer_depth", self._buffer.depth())
        self._metrics.set("carto_edge_buffer_bytes", self._buffer.bytes())
        oldest = self._buffer.oldest_at()
        age = 0.0 if oldest is None else max(0.0, (self._clock() - oldest).total_seconds())
        self._metrics.set("carto_edge_buffer_oldest_age_seconds", age)

    def run(self, stop: threading.Event) -> None:
        """Send until ``stop`` is set; every wait returns as soon as it is."""
        log.info("forward.started")
        while not stop.is_set():
            try:
                report = self.send_once()
            except Exception as exc:  # the loop must survive a buffer or client fault
                log.error("forward.loop_error", error=type(exc).__name__)
                report = SendReport(failed=True, retry_after_seconds=self._backoff())
            try:
                self.refresh_gauges()
            except Exception as exc:  # gauges are best effort; delivery goes on
                log.warning("forward.gauges_error", error=type(exc).__name__)
            if report.idle:
                stop.wait(IDLE_WAIT_SECONDS)
            elif report.failed:
                stop.wait(max(MIN_RETRY_WAIT_SECONDS, report.retry_after_seconds or 0.0))
        log.info("forward.stopped")

    def drain(self, *, max_failures: int = 3) -> int:
        """Send until the buffer is idle or ``max_failures`` consecutive attempts failed,
        sleeping the backoff between failures (for a clean shutdown); returns batches sent."""
        sent = 0
        failures = 0
        while True:
            report = self.send_once()
            if report.idle:
                return sent
            if report.failed:
                failures += 1
                if failures >= max_failures:
                    return sent
                self._sleep(report.retry_after_seconds or 0.0)
                continue
            failures = 0
            if report.sent:
                sent += 1


# ---------------------------------------------------------------------------------------------
# Source health and heartbeats
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceHealthState:
    """One source's health as of a :meth:`SourceHealth.snapshot` (spec 8.1 "Health")."""

    source_id: str
    status: SourceStatus
    last_success_at: datetime | None
    last_error_at: datetime | None
    lag_seconds: float | None
    error_count: int
    records: int


@dataclass(slots=True)
class _Health:
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    lag_seconds: float | None = None
    error_count: int = 0
    records: int = 0


def _status(error_count: int) -> SourceStatus:
    if error_count >= FAILING_AFTER_ERRORS:
        return SourceStatus.FAILING
    if error_count > 0:
        return SourceStatus.DEGRADED
    return SourceStatus.OK


class SourceHealth:
    """Thread-safe per-source read health, fed by the poll scheduler and the receivers."""

    __slots__ = ("_lock", "_sources")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sources: dict[str, _Health] = {}

    def __repr__(self) -> str:
        return f"SourceHealth(sources={len(self._sources)})"

    def record_read(
        self, source_id: str, records: int, lag_seconds: float | None, at: datetime
    ) -> None:
        """A successful read of ``records`` records; resets the consecutive error count."""
        with self._lock:
            health = self._sources.setdefault(source_id, _Health())
            health.last_success_at = at
            health.lag_seconds = (
                max(0.0, float(lag_seconds))
                if lag_seconds is not None and math.isfinite(lag_seconds)
                else None
            )
            health.error_count = 0
            health.records += max(0, records)

    def record_error(self, source_id: str, at: datetime | None = None) -> None:
        """A failed read; three in a row make the source ``failing``."""
        with self._lock:
            health = self._sources.setdefault(source_id, _Health())
            health.error_count += 1
            if at is not None:
                health.last_error_at = at

    def status(self, source_id: str) -> SourceStatus:
        with self._lock:
            health = self._sources.get(source_id)
            return _status(health.error_count if health is not None else 0)

    def state(self, source_id: str) -> SourceHealthState:
        """The health of one source; a source never seen is ``ok`` with nothing recorded."""
        with self._lock:
            health = self._sources.get(source_id, _Health())
            return self._freeze(source_id, health)

    def snapshot(self) -> dict[str, SourceHealthState]:
        with self._lock:
            return {
                source_id: self._freeze(source_id, health)
                for source_id, health in self._sources.items()
            }

    @staticmethod
    def _freeze(source_id: str, health: _Health) -> SourceHealthState:
        return SourceHealthState(
            source_id=source_id,
            status=_status(health.error_count),
            last_success_at=health.last_success_at,
            last_error_at=health.last_error_at,
            lag_seconds=health.lag_seconds,
            error_count=health.error_count,
            records=health.records,
        )


class Heartbeater:
    """Posts a :class:`SourceHeartbeat` per enabled source (spec 8.1, 12)."""

    __slots__ = ("_buffer", "_client", "_clock", "_health", "_metrics", "_sources", "_tenant_id")

    def __init__(
        self,
        client: httpx.Client,
        tenant_id: str,
        sources: SourcesFile,
        buffer: DiskBuffer,
        health: SourceHealth,
        *,
        metrics: EdgeMetrics,
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._tenant_id = tenant_id
        self._sources = sources
        self._buffer = buffer
        self._health = health
        self._metrics = metrics
        self._clock = clock if clock is not None else _utc_now

    def __repr__(self) -> str:
        return f"Heartbeater(tenant_id={self._tenant_id!r}, sources={len(self._sources.sources)})"

    def beats(self) -> list[SourceHeartbeat]:
        """The heartbeats one round would send, in sources-file order."""
        now = self._clock()
        depth = self._buffer.depth()
        oldest = self._buffer.oldest_at()
        out: list[SourceHeartbeat] = []
        for source in self._sources.sources:
            if not source.enabled:
                continue
            state = self._health.state(source.id)
            out.append(
                SourceHeartbeat(
                    schema_version=SCHEMA_VERSION,
                    tenant_id=self._tenant_id,
                    source_id=source.id,
                    sent_at=now,
                    status=state.status,
                    last_success_at=state.last_success_at,
                    lag_seconds=float(state.lag_seconds or 0.0),
                    error_count=state.error_count,
                    buffer_depth=depth,
                    oldest_buffered_at=oldest,
                    message="",
                )
            )
        return out

    def send(self) -> int:
        """One round; returns how many heartbeats core accepted. Never raises for a failed
        post: failures are logged by status or exception type and retried next round."""
        accepted = 0
        for beat in self.beats():
            try:
                response = self._client.post(
                    HEARTBEAT_PATH,
                    content=beat.model_dump_json().encode("utf-8"),
                    headers={"Content-Type": _JSON, "X-Request-Id": new_ulid()},
                )
            except httpx.HTTPError as exc:
                log.warning(
                    "heartbeat.transport_error", error=type(exc).__name__, source_id=beat.source_id
                )
                continue
            if 200 <= response.status_code < 300:
                accepted += 1
                self._metrics.inc("carto_edge_heartbeats_total")
            else:
                log.warning(
                    "heartbeat.refused", status=response.status_code, source_id=beat.source_id
                )
        return accepted

    def run(self, stop: threading.Event, interval_seconds: float) -> None:
        """A round now, then one every ``interval_seconds`` until ``stop`` is set."""
        while not stop.is_set():
            try:
                self.send()
            except Exception as exc:  # the loop must survive a buffer or encoding fault
                log.error("heartbeat.loop_error", error=type(exc).__name__)
            stop.wait(interval_seconds)
