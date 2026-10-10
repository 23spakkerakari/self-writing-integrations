"""The edge-gateway FastAPI application (spec 8.1.2, 8.1.6, 8.4, 8.5, 12, 14.12, 16).

Routes, all behind mutual TLS (:mod:`.server`):

- ``POST /v1/logs``: the OTLP/HTTP logs receiver (protobuf, ``gzip``, ``zstd``) the collector
  exports to. Backpressure first (spec 8.5: ``429`` with ``Retry-After: 5`` above the buffer's
  backpressure ratio, ``503`` with ``Retry-After: 30`` when it is full, so the collector keeps
  the data in its persistent queue), then the media type and encoding (``415``), the wire cap
  (``413``), :func:`~carto_edge.gateway.otlp.parse_logs_request` (decompressed cap ``413``,
  malformed ``400``) and the ingestor. The answer is an ``ExportLogsServiceResponse`` whose
  ``partial_success`` counts the records of untagged or unknown sources.
- ``POST /webhooks/{source_id}``: the webhook receiver (spec 8.1.6) for enabled ``webhook``
  sources; ``404`` for any other id, the same backpressure codes, the body capped at the
  smaller of ``gateway.webhook_max_body_bytes`` and the connector's ``max_body_bytes``, the
  HMAC secret resolved per request (``503`` when it cannot be), ``401`` on a missing or bad
  signature, ``400`` on a body that is not a JSON object or array of objects, then ``202``.
- ``POST /internal/tokenize`` and ``POST /internal/reveal`` (spec 8.4, 12): JSON
  :class:`~carto_edge.reveal.TokenizeRequest` / :class:`~carto_edge.reveal.RevealRequest` to
  :class:`~carto_edge.reveal.RevealService`, whose typed errors map to ``401``, ``413``,
  ``429`` (with ``Retry-After``) and ``503``.
- ``GET /healthz``, ``GET /readyz`` (no detail), ``GET /metrics`` (Prometheus text, internal
  network only).

Errors are RFC 7807 problems (``application/problem+json``) with fixed titles and details:
they never echo a body, a header value or the raw request path (``instance`` is the route
template, empty when no route matched). Validation problems list field locations only. Every
response carries ``X-Request-Id`` (a well-formed incoming id is echoed, otherwise a ULID is
generated), the id is bound into the log context, and ``carto_edge_requests_total`` counts
every response by route template and status. An unhandled exception becomes a ``500`` problem
and is logged by type name only. Nothing here logs a body, a header value, a token or a query
string (spec 2.3 invariant 7, 14.12).

The webhook secret is resolved in the thread pool: a ``vault://`` lookup is network I/O and
must not stall the event loop (every other route would wait); the resolver is thread-safe,
caches values (spec 14.3) and caches failures briefly. A secret shorter than
:data:`MIN_WEBHOOK_SECRET_LEN` is refused (503) rather than used: an empty secret would make the
HMAC check pass for anyone. At most :data:`OTLP_CONCURRENCY` OTLP requests are decoded at once,
bounding the memory a burst of large requests can take.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Final

import structlog.contextvars
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_edge.config import EdgeSettings, SourceType
from carto_edge.connectors.base import ConnectorContext, InvalidConfigError
from carto_edge.connectors.registry import build_connector
from carto_edge.connectors.webhook import WebhookBodyError, WebhookConnector
from carto_edge.gateway.otlp import OTLP_ENCODINGS, OtlpBodyError, parse_logs_request
from carto_edge.metrics import EdgeMetrics
from carto_edge.pipeline.batch import buffer_refused
from carto_edge.pipeline.forward import SourceHealth
from carto_edge.pipeline.ingestor import BackpressureState, IngestorLike, IngestOutcome
from carto_edge.pipeline.model import RawRecord
from carto_edge.reveal import (
    AssertionRejected,
    RateLimited,
    RequestTooLarge,
    RevealDisabled,
    RevealRequest,
    RevealService,
    TokenizeRequest,
)
from carto_edge.runtime import EdgeRuntime, build_connector_context

__all__ = [
    "FULL_RETRY_AFTER_SECONDS",
    "INTERNAL_MAX_BODY_BYTES",
    "OTLP_MEDIA_TYPE",
    "PROBLEM_CONTENT_TYPE",
    "REQUESTS_METRIC",
    "REQUEST_ID_HEADER",
    "SLOW_RETRY_AFTER_SECONDS",
    "UNMATCHED_ROUTE",
    "GatewayMiddleware",
    "Problem",
    "create_gateway_app",
    "read_capped_body",
]

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"
REQUEST_ID_HEADER: Final = "x-request-id"
OTLP_MEDIA_TYPE: Final = "application/x-protobuf"
JSON_MEDIA_TYPE: Final = "application/json"
METRICS_MEDIA_TYPE: Final = "text/plain; version=0.0.4"
REQUESTS_METRIC: Final = "carto_edge_requests_total"
MIN_WEBHOOK_SECRET_LEN: Final = 16
"""Shortest HMAC secret accepted (the source test asks for the same)."""
OTLP_CONCURRENCY: Final = 4
"""OTLP requests decoded and ingested at the same time."""
UNMATCHED_ROUTE: Final = "unmatched"
SLOW_RETRY_AFTER_SECONDS: Final = 5
FULL_RETRY_AFTER_SECONDS: Final = 30
INTERNAL_MAX_BODY_BYTES: Final = 64 * 1024
"""``/internal/*`` bodies: an assertion (at most 8 KiB) plus at most 100 tokens or one query."""

_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IDENTITY: Final = frozenset({"", "identity"})
_NO_STORE: Final = {"cache-control": "no-store"}
_OTLP_REJECTED_MESSAGE: Final = (
    "log records without the carto.source_id resource attribute of an enabled otlp source, "
    "or over the per-request record limit"
)

log = get_logger(component="edge-gateway")

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


class Problem(Exception):  # noqa: N818 - an RFC 7807 problem document, raised to answer with
    """An RFC 7807 problem to answer with. ``detail`` never carries request content."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str = "",
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {title}")
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors
        self.headers = dict(headers) if headers is not None else None


# ---------------------------------------------------------------------------------------------
# Request ids, request counting and the last-resort 500
# ---------------------------------------------------------------------------------------------


def _route_template(scope: Scope) -> str | None:
    """The matched route's path template (``/webhooks/{source_id}``), never the raw path."""
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) else None


def _problem_body(
    status: int, title: str, detail: str, instance: str, request_id: str
) -> dict[str, Any]:
    return {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id,
    }


class GatewayMiddleware:
    """Echo a well-formed ``X-Request-Id`` or generate a ULID and bind it to the log context;
    count every response in ``carto_edge_requests_total{route,status}``; turn an exception
    that escaped every handler into a ``500`` problem logged by type name only."""

    def __init__(self, app: ASGIApp, metrics: EdgeMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else new_ulid()
        scope.setdefault("state", {})["request_id"] = request_id
        statuses: list[int] = []

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                statuses.append(int(message["status"]))
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:  # the last resort: answer 500, log the type name only
            log.error(
                "request.unhandled_error",
                error=type(exc).__name__,
                route=_route_template(scope) or UNMATCHED_ROUTE,
            )
            if not statuses:
                await self._send_500(scope, request_id, send_with_id)
        finally:
            route = _route_template(scope) or UNMATCHED_ROUTE
            if statuses:
                self.metrics.inc(REQUESTS_METRIC, route=route, status=str(statuses[0]))
            # The access line replaces uvicorn's, which carries the raw path and query string
            # (spec 14.12): the route template, method, status and duration only.
            log.info(
                "request",
                method=str(scope.get("method", "")),
                route=route,
                status=statuses[0] if statuses else 0,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            structlog.contextvars.reset_contextvars(**tokens)

    @staticmethod
    async def _send_500(scope: Scope, request_id: str, send: Send) -> None:
        instance = _route_template(scope) or ""
        body = json.dumps(_problem_body(500, "internal error", "", instance, request_id)).encode(
            "utf-8"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", PROBLEM_CONTENT_TYPE.encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", "")
    return str(value) if value else ""


def _problem_response(request: Request, problem: Problem) -> JSONResponse:
    body = _problem_body(
        problem.status,
        problem.title,
        problem.detail,
        _route_template(request.scope) or "",
        _request_id(request),
    )
    if problem.errors is not None:
        body["errors"] = problem.errors
    return JSONResponse(
        body,
        status_code=problem.status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=problem.headers,
    )


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _content_encoding(request: Request) -> str:
    return request.headers.get("content-encoding", "").strip().lower()


async def read_capped_body(request: Request, limit: int) -> bytes:
    """The request body, refused with ``413`` as soon as it (or its declared length) exceeds
    ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise Problem(400, "bad request", "content-length is not a number") from None
        if length > limit:
            raise Problem(413, "payload too large", "body exceeds the size limit")
    buffer = bytearray()
    async for chunk in request.stream():
        buffer += chunk
        if len(buffer) > limit:
            raise Problem(413, "payload too large", "body exceeds the size limit")
    return bytes(buffer)


async def _ingest_durably(
    ingestor: IngestorLike,
    records: list[RawRecord],
    health: SourceHealth | None,
    now: Callable[[], datetime],
) -> IngestOutcome:
    """Ingest pushed records and seal them into the disk buffer before answering: a 2xx tells
    the sender it may forget them (spec 8.1 at-least-once). A batch the buffer refused, or any
    failure while storing, is a 503 with ``Retry-After``: the OTLP exporter retries 503 and keeps
    the request in its persistent queue, where a 500 would make it drop the data (spec 8.5).
    Pushed sources report their reads to source health so heartbeats are not "ok, no data"."""
    try:
        outcome = await run_in_threadpool(ingestor.ingest, records, durable=True)
    except Exception as exc:  # the sender must retry, never drop
        log.error("push.store_failed", error=type(exc).__name__, records=len(records))
        raise Problem(
            503,
            "service unavailable",
            "the edge could not store the records; retry later",
            headers={"retry-after": str(FULL_RETRY_AFTER_SECONDS)},
        ) from None
    if buffer_refused(outcome):
        raise Problem(
            503,
            "service unavailable",
            "the edge buffer is full; retry later",
            headers={"retry-after": str(FULL_RETRY_AFTER_SECONDS)},
        )
    if health is not None:
        at = now()
        for source_id, count in Counter(record.source_id for record in records).items():
            health.record_read(source_id, count, None, at)
    return outcome


async def _check_backpressure(ingestor: IngestorLike) -> None:
    """Spec 8.5: push receivers answer 429/503 so upstream collectors retain the data."""
    state = await run_in_threadpool(ingestor.backpressure)
    if state == BackpressureState.SLOW:
        raise Problem(
            429,
            "too many requests",
            "the edge buffer is above its backpressure threshold; retry later",
            headers={"retry-after": str(SLOW_RETRY_AFTER_SECONDS)},
        )
    if state == BackpressureState.FULL:
        raise Problem(
            503,
            "service unavailable",
            "the edge buffer is full; retry later",
            headers={"retry-after": str(FULL_RETRY_AFTER_SECONDS)},
        )


def _safe_location(location: tuple[int | str, ...], known: frozenset[str]) -> list[int | str]:
    """A validation error location with any name the model does not define (an extra key,
    which is request content) replaced by ``*``."""
    return [piece if isinstance(piece, int) or piece in known else "*" for piece in location]


def _parse_model[T: BaseModel](model: type[T], data: bytes) -> T:
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        if errors and all(error["type"] == "json_invalid" for error in errors):
            raise Problem(400, "bad request", "body is not valid JSON") from None
        known = frozenset(model.model_fields)
        locations = [{"loc": _safe_location(error["loc"], known)} for error in errors]
        raise Problem(
            422, "request body failed validation", "see errors", errors=locations
        ) from None


async def _internal_body(request: Request) -> bytes:
    media_type = _media_type(request)
    if media_type and media_type != JSON_MEDIA_TYPE:
        raise Problem(415, "unsupported media type", f"send {JSON_MEDIA_TYPE}")
    if _content_encoding(request) not in _IDENTITY:
        raise Problem(415, "unsupported content encoding", "send no content encoding")
    return await read_capped_body(request, INTERNAL_MAX_BODY_BYTES)


async def _call_reveal[R, S](func: Callable[[R], S], request_model: R, action: str) -> S:
    """Run a :class:`RevealService` call in the thread pool and map its typed errors."""
    try:
        return await run_in_threadpool(func, request_model)
    except AssertionRejected:
        log.warning("internal.assertion_rejected", action=action)
        raise Problem(401, "unauthorized", "assertion rejected") from None
    except RequestTooLarge:
        log.info("internal.request_too_large", action=action)
        raise Problem(413, "payload too large", "too many tokens in one request") from None
    except RateLimited as exc:
        log.info("internal.rate_limited", action=action)
        retry_after = max(1, exc.retry_after_seconds)
        raise Problem(
            429,
            "too many requests",
            "the hourly quota for this subject is exhausted",
            headers={"retry-after": str(retry_after)},
        ) from None
    except RevealDisabled:
        log.warning("internal.disabled", action=action)
        raise Problem(503, "service unavailable", "no assertion public key is installed") from None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _always_ready() -> bool:
    return True


def _webhook_connectors(
    runtime: EdgeRuntime,
) -> tuple[dict[str, WebhookConnector], ConnectorContext | None]:
    """The connectors of the enabled webhook sources, built once, and their context."""
    wanted = [
        source
        for source in runtime.sources.sources
        if source.type == SourceType.WEBHOOK and source.enabled
    ]
    if not wanted:
        return {}, None
    context = build_connector_context(runtime)
    connectors: dict[str, WebhookConnector] = {}
    for source in wanted:
        try:
            connector = build_connector(source, context)
        except InvalidConfigError as exc:
            log.warning("webhook.source_invalid", source_id=source.id, error=type(exc).__name__)
            continue
        if isinstance(connector, WebhookConnector):
            connectors[source.id] = connector
    return connectors, context


# ---------------------------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------------------------


def create_gateway_app(
    settings: EdgeSettings,
    runtime: EdgeRuntime,
    ingestor: IngestorLike,
    reveal: RevealService,
    metrics: EdgeMetrics,
    *,
    ready: Callable[[], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
    lifespan: Lifespan | None = None,
    health: SourceHealth | None = None,
) -> FastAPI:
    """Build the application; the caller owns ``runtime``, ``ingestor`` and ``reveal``."""
    now = clock if clock is not None else _utc_now
    ready_check = ready if ready is not None else _always_ready
    gateway = settings.gateway
    otlp_systems = {
        source.id: source.system
        for source in runtime.sources.sources
        if source.type == SourceType.OTLP and source.enabled
    }
    webhooks, context = _webhook_connectors(runtime)
    metrics.counter(REQUESTS_METRIC, "HTTP requests answered, by route and status.")

    otlp_slots = asyncio.Semaphore(OTLP_CONCURRENCY)
    app = FastAPI(
        title="carto edge-gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.add_middleware(GatewayMiddleware, metrics=metrics)

    @app.exception_handler(Problem)
    async def on_problem(request: Request, exc: Problem) -> Response:
        return _problem_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        titles = {404: "not found", 405: "method not allowed"}
        problem = Problem(
            exc.status_code,
            titles.get(exc.status_code, "request refused"),
            headers=exc.headers,
        )
        return _problem_response(request, problem)

    @app.exception_handler(RequestValidationError)
    async def on_request_validation(request: Request, _exc: RequestValidationError) -> Response:
        return _problem_response(request, Problem(422, "request failed validation"))

    # -- OTLP -----------------------------------------------------------------------------------

    @app.post("/v1/logs")
    async def otlp_logs(request: Request) -> Response:
        await _check_backpressure(ingestor)
        if _media_type(request) != OTLP_MEDIA_TYPE:
            raise Problem(415, "unsupported media type", f"send {OTLP_MEDIA_TYPE}")
        encoding = _content_encoding(request)
        if encoding not in OTLP_ENCODINGS:
            raise Problem(
                415, "unsupported content encoding", "send gzip, zstd or no content encoding"
            )
        async with otlp_slots:
            return await _otlp_decode_and_ingest(request, encoding)

    async def _otlp_decode_and_ingest(request: Request, encoding: str) -> Response:
        body = await read_capped_body(request, gateway.otlp_max_body_bytes)
        try:
            parsed = await run_in_threadpool(
                parse_logs_request,
                body,
                content_encoding=encoding,
                max_bytes=gateway.otlp_max_body_bytes,
                received_at=now(),
                system_of=otlp_systems,
            )
        except OtlpBodyError as exc:
            log.warning("otlp.body_rejected", reason=exc.reason)
            if exc.reason == "too_large":
                raise Problem(
                    413, "payload too large", "decompressed body exceeds the cap"
                ) from None
            if exc.reason == "too_many":
                raise Problem(
                    413, "payload too large", "too many log records in one request"
                ) from None
            raise Problem(400, "bad request", "body is not a readable OTLP logs request") from None
        del body
        records = parsed.all_records()
        events = 0
        if records:
            outcome = await _ingest_durably(ingestor, records, health, now)
            events = outcome.events
        log.info(
            "otlp.received",
            accepted=parsed.accepted,
            rejected=parsed.rejected,
            sources=len(parsed.records),
            events=events,
        )
        answer = logs_service_pb2.ExportLogsServiceResponse()
        if parsed.rejected:
            answer.partial_success.rejected_log_records = parsed.rejected
            answer.partial_success.error_message = _OTLP_REJECTED_MESSAGE
        return Response(answer.SerializeToString(), media_type=OTLP_MEDIA_TYPE)

    # -- Webhooks -------------------------------------------------------------------------------

    @app.post("/webhooks/{source_id}")
    async def webhook(source_id: str, request: Request) -> Response:
        connector = webhooks.get(source_id)
        if connector is None or context is None:
            raise Problem(404, "not found", "unknown source")
        await _check_backpressure(ingestor)
        if _content_encoding(request) not in _IDENTITY:
            raise Problem(415, "unsupported content encoding", "send no content encoding")
        limit = min(gateway.webhook_max_body_bytes, connector.config.max_body_bytes)
        body = await read_capped_body(request, limit)
        try:
            secret = await run_in_threadpool(
                context.secrets.resolve, connector.source.secret_ref or ""
            )
        except Exception as exc:  # any resolver failure is the same 503
            log.warning("webhook.secret_unavailable", source_id=source_id, error=type(exc).__name__)
            raise Problem(503, "service unavailable", "secret unavailable") from None
        if len(secret) < MIN_WEBHOOK_SECRET_LEN:
            log.warning("webhook.secret_too_short", source_id=source_id)
            raise Problem(503, "service unavailable", "secret unavailable")
        received_at = now()
        if not connector.verify_signature(secret, body, request.headers, received_at):
            log.warning("webhook.signature_rejected", source_id=source_id)
            raise Problem(401, "unauthorized", "signature missing or rejected")
        try:
            records = await run_in_threadpool(connector.records_from_body, body, received_at)
        except WebhookBodyError:
            log.info("webhook.body_rejected", source_id=source_id)
            raise Problem(
                400, "bad request", "body must be a JSON object or an array of objects"
            ) from None
        del body
        outcome = await _ingest_durably(ingestor, records, health, now)
        log.info(
            "webhook.received", source_id=source_id, records=len(records), events=outcome.events
        )
        return JSONResponse({"accepted": len(records)}, status_code=202)

    # -- Internal tokenize and reveal -----------------------------------------------------------

    @app.post("/internal/tokenize")
    async def internal_tokenize(request: Request) -> Response:
        parsed = _parse_model(TokenizeRequest, await _internal_body(request))
        result = await _call_reveal(reveal.tokenize, parsed, "tokenize")
        return JSONResponse(result.model_dump(mode="json"), headers=_NO_STORE)

    @app.post("/internal/reveal")
    async def internal_reveal(request: Request) -> Response:
        parsed = _parse_model(RevealRequest, await _internal_body(request))
        result = await _call_reveal(reveal.reveal, parsed, "reveal")
        return JSONResponse(result.model_dump(mode="json"), headers=_NO_STORE)

    # -- Health and metrics ---------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> Response:
        if await run_in_threadpool(ready_check):
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/metrics")
    async def metrics_text() -> Response:
        return PlainTextResponse(metrics.render(), media_type=METRICS_MEDIA_TYPE)

    return app
