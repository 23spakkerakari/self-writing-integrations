"""The ``ingest-api`` FastAPI application (spec 5.3, 8.5, 12, 14.12, 16).

Endpoints, all internal (mutual TLS, never behind the ingress; see :mod:`.server`):

- ``POST /internal/ingest``: one :class:`~carto_schema.ingest.IngestBatch` as JSON, optionally
  ``Content-Encoding: zstd``. The body is capped on the wire and after decompression (spec 2.3
  invariant 8). Only the contract models are accepted, so a raw value cannot arrive in a slot the
  schema does not have (spec 2.3 invariant 2). A batch whose id the ledger knows is acknowledged
  as a duplicate and not rewritten; otherwise the order is ledger lookup, ClickHouse write,
  ledger insert (ADR 0015), and a failed write is a 503 so the edge retries.
- ``POST /internal/heartbeat``: one :class:`~carto_schema.ingest.SourceHeartbeat`, upserted into
  the source health store.
- ``GET /healthz``, ``GET /readyz`` (no detail in the body), ``GET /metrics`` (Prometheus text).

Errors are RFC 7807 problem documents (``application/problem+json``) that carry locations and
messages but never the rejected input. Every response carries ``X-Request-Id`` (echoed when the
caller's is well formed, generated otherwise) and the id is bound into the log context. Request
and response bodies are never logged (spec 2.3 invariant 7, 14.12); the logger only ever sees
ids and counts.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import structlog.contextvars
import zstandard
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_core.db.clickhouse import WriterError
from carto_core.ingest.health import HealthStoreError, SourceHealthRecord
from carto_core.ingest.ledger import LedgerEntry, LedgerError
from carto_schema.ingest import IngestBatch, SourceHeartbeat

if TYPE_CHECKING:
    from carto_core.db.clickhouse import EventWriter
    from carto_core.ingest.health import SourceHealthStore
    from carto_core.ingest.ledger import BatchLedger
    from carto_core.settings import CoreSettings, IngestListenerSettings

__all__ = [
    "METRIC_NAMES",
    "PROBLEM_CONTENT_TYPE",
    "REQUEST_ID_HEADER",
    "IngestMetrics",
    "Problem",
    "RequestIdMiddleware",
    "create_app",
    "decompress_capped",
]

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"
REQUEST_ID_HEADER: Final = "x-request-id"
_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DECOMPRESS_CHUNK: Final = 64 * 1024
_JSON_MEDIA_TYPE: Final = "application/json"

METRIC_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("carto_ingest_batches_total", "Batches accepted and written to ClickHouse."),
    ("carto_ingest_events_total", "Events written to ClickHouse."),
    ("carto_ingest_duplicates_total", "Batches acknowledged as already in the ledger."),
    ("carto_ingest_errors_total", "Internal requests answered with a problem (4xx or 5xx)."),
    ("carto_heartbeats_total", "Source heartbeats recorded."),
)

log = get_logger(component="ingest-api")


class Problem(Exception):  # noqa: N818 - an RFC 7807 problem document, raised to answer with
    """An RFC 7807 problem to answer with. ``detail`` never carries request content."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str = "",
        *,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(f"{status} {title}")
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors


class IngestMetrics:
    """Hand-rolled counters rendered in the Prometheus text format (spec 16)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {name: 0 for name, _help in METRIC_NAMES}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
        lines: list[str] = []
        for name, help_text in METRIC_NAMES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {values[name]}")
        return "\n".join(lines) + "\n"


class RequestIdMiddleware:
    """Echo a well-formed ``X-Request-Id`` or generate a ULID; bind it to the log context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else new_ulid()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)


def decompress_capped(data: bytes, limit: int) -> bytes:
    """zstd-decompress ``data``; a result above ``limit`` bytes is a 413, a bad frame a 400."""
    out = bytearray()
    try:
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
            while True:
                chunk = reader.read(_DECOMPRESS_CHUNK)
                if not chunk:
                    break
                out += chunk
                if len(out) > limit:
                    raise Problem(413, "payload too large", "decompressed body exceeds the cap")
    except zstandard.ZstdError as exc:
        raise Problem(400, "bad request", "malformed zstd body") from exc
    return bytes(out)


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", "")
    return str(value) if value else ""


def _problem_response(request: Request, problem: Problem) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "instance": request.url.path,
        "request_id": _request_id(request),
    }
    if problem.errors is not None:
        body["errors"] = problem.errors
    return JSONResponse(body, status_code=problem.status, media_type=PROBLEM_CONTENT_TYPE)


def _validation_problem(exc: ValidationError) -> Problem:
    """422 with locations, messages and types; the rejected input stays out (spec 14.12)."""
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    if errors and all(error["type"] == "json_invalid" for error in errors):
        return Problem(400, "bad request", "body is not valid JSON")
    return Problem(422, "request body failed validation", "see errors", errors=errors)


def _parse[T: BaseModel](model: type[T], data: bytes) -> T:
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        raise _validation_problem(exc) from None


async def _read_body(request: Request, listener: IngestListenerSettings) -> bytes:
    """Read a capped body; honour ``Content-Encoding: zstd`` with the decompressed cap."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type and media_type != _JSON_MEDIA_TYPE:
        raise Problem(415, "unsupported media type", f"send {_JSON_MEDIA_TYPE}")
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity", "zstd"):
        raise Problem(415, "unsupported content encoding", "send zstd or no content encoding")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise Problem(400, "bad request", "content-length is not a number") from None
        if length > listener.max_body_bytes:
            raise Problem(413, "payload too large", "body exceeds the wire cap")
    buffer = bytearray()
    async for chunk in request.stream():
        buffer += chunk
        if len(buffer) > listener.max_body_bytes:
            raise Problem(413, "payload too large", "body exceeds the wire cap")
    if not buffer:
        raise Problem(400, "bad request", "empty body")
    data = bytes(buffer)
    if encoding == "zstd":
        data = decompress_capped(data, listener.max_decompressed_bytes)
    return data


def create_app(
    settings: CoreSettings,
    writer: EventWriter,
    ledger: BatchLedger,
    health: SourceHealthStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the application with its stores injected (tests pass in-memory ones)."""
    now = clock if clock is not None else lambda: datetime.now(UTC)
    listener = settings.ingest
    tenant_id = settings.tenant_id
    metrics = IngestMetrics()
    app = FastAPI(title="carto ingest-api", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestIdMiddleware)
    app.state.metrics = metrics

    def process_batch(batch: IngestBatch) -> bool:
        """Ledger lookup, write, ledger insert (ADR 0015). Returns True for a duplicate."""
        context = {
            "tenant_id": batch.tenant_id,
            "source_id": batch.source_id,
            "batch_id": batch.batch_id,
            "events": len(batch.events),
        }
        try:
            if ledger.contains(batch.tenant_id, batch.batch_id):
                metrics.inc("carto_ingest_duplicates_total")
                log.info("batch already in the ledger", **context)
                return True
            result = writer.write_events(batch.events)
            recorded = ledger.record(
                LedgerEntry(
                    batch.tenant_id, batch.batch_id, batch.source_id, len(batch.events), now()
                )
            )
        except WriterError as exc:
            log.warning("batch write failed", error=type(exc).__name__, **context)
            raise Problem(503, "event store unavailable", "retry the batch later") from exc
        except LedgerError as exc:
            log.warning("batch ledger failed", error=type(exc).__name__, **context)
            raise Problem(503, "ledger unavailable", "retry the batch later") from exc
        metrics.inc("carto_ingest_batches_total")
        metrics.inc("carto_ingest_events_total", result.events)
        log.info("batch accepted", identifiers=result.identifiers, raced=not recorded, **context)
        return False

    @app.exception_handler(Problem)
    async def on_problem(request: Request, exc: Problem) -> Response:
        if request.url.path.startswith("/internal/"):
            metrics.inc("carto_ingest_errors_total")
        return _problem_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        title = "not found" if exc.status_code == 404 else "request refused"
        return _problem_response(request, Problem(exc.status_code, title))

    @app.exception_handler(RequestValidationError)
    async def on_request_validation(request: Request, _exc: RequestValidationError) -> Response:
        return _problem_response(request, Problem(422, "request failed validation"))

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> Response:
        log.error("unhandled error", error=type(exc).__name__, path=request.url.path)
        return _problem_response(request, Problem(500, "internal error"))

    @app.post("/internal/ingest")
    async def ingest(request: Request) -> Response:
        body = await _read_body(request, listener)
        batch = _parse(IngestBatch, body)
        del body
        if batch.tenant_id != tenant_id:
            raise Problem(403, "forbidden", "batch is for a tenant this core does not serve")
        duplicate = await run_in_threadpool(process_batch, batch)
        return JSONResponse({"accepted": len(batch.events), "duplicate": duplicate})

    @app.post("/internal/heartbeat")
    async def heartbeat(request: Request) -> Response:
        body = await _read_body(request, listener)
        beat = _parse(SourceHeartbeat, body)
        del body
        if beat.tenant_id != tenant_id:
            raise Problem(403, "forbidden", "heartbeat is for a tenant this core does not serve")
        record = SourceHealthRecord.from_heartbeat(beat, now())
        try:
            await run_in_threadpool(health.upsert, record)
        except HealthStoreError as exc:
            log.warning(
                "heartbeat store failed",
                error=type(exc).__name__,
                tenant_id=beat.tenant_id,
                source_id=beat.source_id,
            )
            raise Problem(503, "health store unavailable", "retry the heartbeat later") from exc
        metrics.inc("carto_heartbeats_total")
        log.info(
            "heartbeat recorded",
            tenant_id=beat.tenant_id,
            source_id=beat.source_id,
            status=beat.status.value,
        )
        return JSONResponse({"recorded": True})

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> Response:
        ready = await run_in_threadpool(lambda: writer.ping() and ledger.ping() and health.ping())
        if ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/metrics")
    async def metrics_text() -> Response:
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    return app
