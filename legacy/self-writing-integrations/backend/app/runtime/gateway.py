"""The runtime gateway executes manifest endpoints.

It owns everything an integration needs at call time: base URL templating, auth injection,
rate limiting, retries, pagination, response validation, and mapping to canonical objects.
Agents never touch credentials; they ask the gateway to call an endpoint on a connection.

Reads go through call(). Writes go through write(): a canonical record is turned into a request
body by the endpoint's request mapping, checked against the request schema before anything is
sent, and retried only when a retry cannot create a duplicate.

Every response is validated against the endpoint's stored schema. Violations are surfaced
as DriftEvents, which is the primary drift signal for APIs that have no spec to re-fetch.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import httpx
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from app.manifest.schema import ApiKeyAuth, Auth, BasicAuth, BearerAuth, Endpoint, IntegrationManifest, OAuth2Auth
from app.runtime.mapping import MappingError, build_request, map_records
from app.runtime.paths import get_path
from app.runtime.secrets import DictSecretsProvider, SecretsProvider


class GatewayError(Exception):
    pass


class ConfigError(GatewayError):
    pass


class Connection(BaseModel):
    """A tenant's binding to an integration: config values for base_url templating.
    Secrets are supplied separately through a SecretsProvider so they never sit in a model
    that might get logged or serialized."""

    tenant_id: str = "default"
    config: dict[str, str] = Field(default_factory=dict)


DriftKind = Literal[
    "schema_violation",  # response failed the stored JSON Schema
    "unexpected_status",  # non-2xx after retries: 401/403 auth, 404 moved, 429 limits, 5xx
    "malformed_body",  # non-JSON body
    "transport_error",  # gave up after connection failures
    "deprecation",  # Sunset/Deprecation headers on a success, or 410 Gone
    "pagination_runaway",  # max_pages exhausted while the API still advertised more
    "mapping_error",  # a record could not be mapped onto its canonical object
    "spec_changed",  # a re-fetched spec differs from the last snapshot (emitted by the drift monitor)
    "shape_changed",  # a scheduled probe saw a field disappear or change type (emitted by the schema watcher)
]

_NOTICE_HEADERS = ("Sunset", "Deprecation", "Link", "Retry-After")


class DriftEvent(BaseModel):
    integration: str
    version: str
    endpoint_id: str
    kind: DriftKind
    detail: str
    status_code: int | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CallResult(BaseModel):
    endpoint_id: str
    ok: bool
    status_code: int | None = None
    url: str = ""
    pages: int = 0
    raw_first_page: Any = None
    records: list[Any] = Field(default_factory=list)
    canonical_object: str | None = None
    canonical: list[dict[str, Any]] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    mapping_errors: list[str] = Field(default_factory=list)
    drift_events: list[DriftEvent] = Field(default_factory=list)


class WriteResult(BaseModel):
    """Outcome of one write. `request_errors` means the body failed its own schema and nothing
    was sent; `ok` is False for those, for non-2xx responses and for transport failures."""

    endpoint_id: str
    ok: bool
    status_code: int | None = None
    url: str = ""
    request_body: Any = None
    response_body: Any = None
    destination_id: str | None = None
    error: str | None = None
    request_errors: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    drift_events: list[DriftEvent] = Field(default_factory=list)


def apply_auth(auth: Auth, secrets: SecretsProvider, query: dict[str, Any], headers: dict[str, str]) -> None:
    """Inject credentials for one request. Shared by the gateway and the discovery prober."""
    if isinstance(auth, ApiKeyAuth):
        value = auth.prefix + secrets.get(auth.secret_ref)
        if auth.location == "header":
            headers[auth.name] = value
        else:
            query[auth.name] = value
    elif isinstance(auth, BearerAuth):
        headers["Authorization"] = "Bearer " + secrets.get(auth.secret_ref)
    elif isinstance(auth, BasicAuth):
        user = auth.username_literal if auth.username_literal is not None else secrets.get(auth.username_secret_ref or "")
        pw = auth.password_literal if auth.password_literal is not None else secrets.get(auth.password_secret_ref or "")
        token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    elif isinstance(auth, OAuth2Auth):
        headers["Authorization"] = "Bearer " + secrets.get("access_token")


class TokenBucket:
    def __init__(self, rate: float, burst: int, sleep: Callable[[float], None], clock: Callable[[], float]):
        self.rate, self.burst, self.sleep, self.clock = rate, burst, sleep, clock
        self.tokens = float(burst)
        self.last = clock()

    def acquire(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            wait = (1 - self.tokens) / self.rate
            self.sleep(wait)
            self.tokens = 0.0
            self.last = self.clock()
        else:
            self.tokens -= 1


class Gateway:
    def __init__(
        self,
        manifest: IntegrationManifest,
        connection: Connection | None = None,
        secrets: SecretsProvider | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 30.0,
    ) -> None:
        self.manifest = manifest
        self.connection = connection or Connection()
        self.secrets = secrets or DictSecretsProvider()
        self._sleep = sleep
        self._bucket = TokenBucket(manifest.rate_limit.requests_per_second, manifest.rate_limit.burst, sleep, clock)
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True)
        self._validators: dict[str, Draft202012Validator] = {}

    # --- public -------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Gateway":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(
        self,
        endpoint_id: str,
        params: dict[str, Any] | None = None,
        paginate: bool = True,
        max_pages: int = 50,
    ) -> CallResult:
        endpoint = self.manifest.endpoint(endpoint_id)
        if self.manifest.request_mapping_for(endpoint_id) is not None:
            raise GatewayError(f"'{endpoint_id}' is a write endpoint; send a canonical record with write()")
        params = dict(params or {})
        result = CallResult(endpoint_id=endpoint_id, ok=True)
        url, query, headers = self._prepare(endpoint, params)
        result.url = url

        page_state: dict[str, Any] = {"page": 1, "offset": 0, "cursor": None}
        more = False
        notice_seen = False
        for page_index in range(max_pages if paginate else 1):
            more = False
            page_query = self._paginated_query(endpoint, query, page_state)
            response = self._request_with_retry(endpoint, url, page_query, headers, result)
            if response is None:
                result.ok = False
                break
            result.status_code = response.status_code
            result.pages += 1
            if not (200 <= response.status_code < 300):
                result.ok = False
                kind: DriftKind = "deprecation" if response.status_code == 410 else "unexpected_status"
                self._drift(result, endpoint, kind, self._status_detail(response), response.status_code)
                break
            if not notice_seen:
                notice = self._deprecation_notice(response)
                if notice:
                    notice_seen = True
                    self._drift(result, endpoint, "deprecation", notice, response.status_code)
            body = self._parse_body(response, endpoint, result)
            if body is None and result.drift_events and result.drift_events[-1].kind == "malformed_body":
                result.ok = False
                break
            if page_index == 0:
                result.raw_first_page = body
            self._validate(endpoint, body, result)
            items = get_path(body, endpoint.items_path) if endpoint.items_path else body
            page_items = items if isinstance(items, list) else ([] if items is None else [items])
            result.records.extend(page_items)
            more = paginate and self._advance(endpoint, body, page_items, page_state)
            if not more:
                break
        if more:
            self._drift(
                result,
                endpoint,
                "pagination_runaway",
                f"stopped after {max_pages} pages while the API still advertised more; "
                f"pagination style '{endpoint.pagination.style}' may no longer match",
                result.status_code,
            )

        mapping = self.manifest.mapping_for(endpoint_id)
        if mapping is not None and result.records:
            canonical, errors = map_records(mapping, result.records, self.manifest.name)
            result.canonical_object = mapping.canonical_object
            result.canonical = [c.model_dump(mode="json") for c in canonical]
            result.mapping_errors = errors
            for message in errors[:20]:
                self._drift(result, endpoint, "mapping_error", message, result.status_code)
        if result.validation_errors or result.mapping_errors:
            result.ok = False
        return result

    def write(
        self,
        endpoint_id: str,
        record: dict[str, Any],
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        """Send one canonical record (as JSON data) to a write endpoint."""
        endpoint = self.manifest.endpoint(endpoint_id)
        mapping = self.manifest.request_mapping_for(endpoint_id)
        if mapping is None:
            raise GatewayError(f"'{endpoint_id}' has no request mapping; it cannot be written to")
        result = WriteResult(endpoint_id=endpoint_id, ok=True)
        url, query, headers = self._prepare(endpoint, dict(params or {}))
        result.url = url

        try:
            body = build_request(mapping, record)
        except MappingError as exc:
            result.ok = False
            result.request_errors.append(str(exc))
            result.error = f"request mapping failed: {exc}"
            return result
        result.request_body = body
        if endpoint.request_schema is not None:
            for err in list(Draft202012Validator(self.manifest.resolve(endpoint.request_schema)).iter_errors(body))[:20]:
                location = "/".join(str(p) for p in err.absolute_path) or "$"
                result.request_errors.append(f"{location}: {err.message}")
            if result.request_errors:
                result.ok = False
                result.error = "request body does not match the endpoint's request schema: " + "; ".join(result.request_errors[:3])
                return result

        # A POST may have been applied even when the response never arrived. Unless the API takes
        # an idempotency key, only failures that prove the request was not processed are retried.
        repeatable = endpoint.method != "POST"
        if endpoint.idempotency_header and idempotency_key:
            headers[endpoint.idempotency_header] = idempotency_key
            repeatable = True
        response = self._request_with_retry(endpoint, url, query, headers, result, json_body=body, repeatable=repeatable)
        if response is None:
            result.ok = False
            result.error = result.drift_events[-1].detail if result.drift_events else "no response"
            return result
        result.status_code = response.status_code
        if not (200 <= response.status_code < 300):
            result.ok = False
            kind: DriftKind = "deprecation" if response.status_code == 410 else "unexpected_status"
            detail = self._status_detail(response)
            self._drift(result, endpoint, kind, detail, response.status_code)
            result.error = detail
            try:
                result.response_body = response.json()
            except ValueError:
                result.response_body = None
            return result
        notice = self._deprecation_notice(response)
        if notice:
            self._drift(result, endpoint, "deprecation", notice, response.status_code)
        parsed = self._parse_body(response, endpoint, result)
        if parsed is None and result.drift_events and result.drift_events[-1].kind == "malformed_body":
            result.ok = False
            result.error = result.drift_events[-1].detail
            return result
        result.response_body = parsed
        self._validate(endpoint, parsed, result)
        if mapping.response_id_path:
            assigned = get_path(parsed, mapping.response_id_path)
            result.destination_id = None if assigned is None else str(assigned)
        return result

    # --- request construction ------------------------------------------------------

    def _prepare(self, endpoint: Endpoint, params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
        try:
            base = self.manifest.base_url.format_map(self.connection.config)
        except KeyError as exc:
            raise ConfigError(f"connection is missing config value {exc} required by base_url") from exc

        declared = {p.name: p for p in endpoint.parameters}
        unknown = set(params) - set(declared)
        if unknown:
            raise GatewayError(f"undeclared parameters for '{endpoint.id}': {sorted(unknown)}")
        for p in endpoint.parameters:
            if p.required and p.name not in params and not (p.location == "query" and p.name in endpoint.default_query):
                raise GatewayError(f"missing required parameter '{p.name}' for '{endpoint.id}'")

        path = endpoint.path
        for p in endpoint.parameters:
            if p.location == "path" and p.name in params:
                path = path.replace("{" + p.name + "}", str(params[p.name]))
        url = base.rstrip("/") + "/" + path.lstrip("/")

        query: dict[str, Any] = dict(endpoint.default_query)
        headers: dict[str, str] = {**self.manifest.default_headers, **endpoint.default_headers}
        for p in endpoint.parameters:
            if p.name in params:
                if p.location == "query":
                    query[p.name] = params[p.name]
                elif p.location == "header":
                    headers[p.name] = str(params[p.name])
        self._apply_auth(query, headers)
        return url, query, headers

    def _apply_auth(self, query: dict[str, Any], headers: dict[str, str]) -> None:
        apply_auth(self.manifest.auth, self.secrets, query, headers)

    def _paginated_query(self, endpoint: Endpoint, query: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        pg = endpoint.pagination
        q = dict(query)
        if pg.style == "page":
            q[pg.page_param] = state["page"]
            if pg.size_param:
                q[pg.size_param] = pg.page_size
        elif pg.style == "offset":
            q[pg.offset_param] = state["offset"]
            q[pg.limit_param] = pg.page_size
        elif pg.style == "cursor":
            if state["cursor"]:
                q[pg.cursor_param] = state["cursor"]
            if pg.size_param:
                q[pg.size_param] = pg.page_size
        return q

    def _advance(self, endpoint: Endpoint, body: Any, items: list[Any], state: dict[str, Any]) -> bool:
        """Update pagination state; return True if another page should be fetched."""
        pg = endpoint.pagination
        if pg.style == "none" or not items:
            return False
        if pg.style == "page":
            if len(items) < pg.page_size:
                return False
            state["page"] += 1
            return True
        if pg.style == "offset":
            if len(items) < pg.page_size:
                return False
            state["offset"] += len(items)
            return True
        if pg.style == "cursor":
            nxt = get_path(body, pg.next_cursor_path) if pg.next_cursor_path else None
            if not nxt:
                return False
            state["cursor"] = nxt
            return True
        return False

    # --- execution ------------------------------------------------------------------

    def _request_with_retry(
        self,
        endpoint: Endpoint,
        url: str,
        query: dict[str, Any],
        headers: dict[str, str],
        result: CallResult | WriteResult,
        json_body: Any = None,
        repeatable: bool = True,
    ) -> httpx.Response | None:
        """`repeatable=False` is for writes that could be applied twice: only a refused connection,
        a rejected token and a 429 are retried, because none of those reached the API's handler."""
        policy = self.manifest.retry
        last_error = ""
        refreshed = False
        attempts = 0
        for attempt in range(1, policy.max_attempts + 1):
            attempts = attempt
            self._bucket.acquire()
            try:
                if json_body is None:
                    response = self._client.request(endpoint.method, url, params=query, headers=headers)
                else:
                    response = self._client.request(endpoint.method, url, params=query, headers=headers, json=json_body)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if not repeatable and not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    break
                self._backoff(attempt, None)
                continue
            if response.status_code == 401 and not refreshed and self._try_refresh():
                # The access token was rejected; the secrets provider obtained a fresh one. Retry once.
                refreshed = True
                self._apply_auth(query, headers)
                continue
            retry_status = response.status_code in policy.retry_on_status and (repeatable or response.status_code == 429)
            if retry_status and attempt < policy.max_attempts:
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            return response
        self._drift(result, endpoint, "transport_error", f"gave up after {attempts} attempt(s): {last_error}")
        return None

    def _try_refresh(self) -> bool:
        """Ask a refreshable secrets provider (the OAuth broker) for a new access token."""
        if not isinstance(self.manifest.auth, OAuth2Auth):
            return False
        refresh = getattr(self.secrets, "refresh", None)
        if refresh is None:
            return False
        try:
            return bool(refresh())
        except Exception:
            return False

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.manifest.retry.backoff_seconds * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 30.0))
            except ValueError:
                pass
        if delay > 0:
            self._sleep(delay)

    def _parse_body(self, response: httpx.Response, endpoint: Endpoint, result: CallResult | WriteResult) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            self._drift(result, endpoint, "malformed_body", f"non-JSON body: {response.text[:200]!r}", response.status_code)
            return None

    @staticmethod
    def _status_detail(response: httpx.Response) -> str:
        detail = f"HTTP {response.status_code}: {response.text[:300]}"
        notices = [f"{h}: {response.headers[h]}" for h in _NOTICE_HEADERS if h in response.headers]
        return detail + (" | " + "; ".join(notices) if notices else "")

    @staticmethod
    def _deprecation_notice(response: httpx.Response) -> str:
        """Sunset (RFC 8594) and Deprecation (RFC 9745) headers announce retirement ahead of time."""
        parts = [f"{h}: {response.headers[h]}" for h in ("Sunset", "Deprecation") if h in response.headers]
        if parts and "Link" in response.headers:
            parts.append(f"Link: {response.headers['Link']}")
        return "; ".join(parts)

    def _validate(self, endpoint: Endpoint, body: Any, result: CallResult | WriteResult) -> None:
        if endpoint.response_schema is None:
            return
        validator = self._validators.get(endpoint.id)
        if validator is None:
            validator = Draft202012Validator(self.manifest.resolve(endpoint.response_schema))
            self._validators[endpoint.id] = validator
        for err in list(validator.iter_errors(body))[:20]:
            location = "/".join(str(p) for p in err.absolute_path) or "$"
            message = f"{location}: {err.message}"
            result.validation_errors.append(message)
            self._drift(result, endpoint, "schema_violation", message, result.status_code)

    def _drift(
        self,
        result: CallResult | WriteResult,
        endpoint: Endpoint,
        kind: DriftKind,
        detail: str,
        status_code: int | None = None,
    ) -> None:
        result.drift_events.append(
            DriftEvent(
                integration=self.manifest.name,
                version=self.manifest.version,
                endpoint_id=endpoint.id,
                kind=kind,
                detail=detail,
                status_code=status_code,
            )
        )
