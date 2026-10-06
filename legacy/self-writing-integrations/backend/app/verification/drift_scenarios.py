"""Drift scenarios: mutations that make the spec-derived mock behave like an API that changed.

A DriftWorld pins the API's behavior to the manifest that was published when the drift appeared
plus a scenario, and serves that behavior no matter which manifest version the client uses. That
is what makes repair verification meaningful: a candidate manifest has to talk correctly to the
world as it is now, not to a mock generated from the candidate's own schema.

Every mutation is request/response middleware around the MockServer, so the mock itself never
learns about drift. Mutations are plain data so the mock control API can accept them over HTTP.
"""
from __future__ import annotations

import base64
import json
from typing import Annotated, Any, Callable, Literal, Union

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.manifest.schema import ApiKeyAuth, BasicAuth, Endpoint, IntegrationManifest
from app.runtime.paths import get_path, set_path
from app.verification.mock_server import MockServer


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenameField(_Strict):
    """Schema drift: a record field was renamed."""

    type: Literal["rename_field"] = "rename_field"
    endpoint_id: str
    old: str
    new: str


class RemoveField(_Strict):
    """Schema drift: a record field disappeared."""

    type: Literal["remove_field"] = "remove_field"
    endpoint_id: str
    field: str


class RetypeField(_Strict):
    """Schema drift: a field changed type; every record gets `value` (e.g. an integer id)."""

    type: Literal["retype_field"] = "retype_field"
    endpoint_id: str
    field: str
    value: Any


class SetField(_Strict):
    """Semantic drift: a field starts carrying a new value, e.g. an enum gained a member."""

    type: Literal["set_field"] = "set_field"
    endpoint_id: str
    field: str
    value: Any


class WrapItems(_Strict):
    """Schema drift: the record list moved under a different top-level key."""

    type: Literal["wrap_items"] = "wrap_items"
    endpoint_id: str
    key: str


class StatusResponse(_Strict):
    """The endpoint now answers with a fixed status: 401/403 auth, 429 limits, 404 gone, 5xx."""

    type: Literal["status"] = "status"
    endpoint_id: str
    status_code: int
    body: Any = Field(default_factory=lambda: {"error": "simulated drift"})
    headers: dict[str, str] = Field(default_factory=dict)


class NonJson(_Strict):
    """Behavioral drift: the endpoint returns an HTML page instead of JSON."""

    type: Literal["non_json"] = "non_json"
    endpoint_id: str


class RequireHeader(_Strict):
    """Auth drift: every request must now carry this header; the old credentials alone get 401."""

    type: Literal["require_header"] = "require_header"
    name: str
    value: str | None = Field(default=None, description="Exact value required; any non-empty value when None")


class PathMoved(_Strict):
    """Deprecation: the old path answers 410 Gone with a Link to its successor, which is served."""

    type: Literal["path_moved"] = "path_moved"
    endpoint_id: str
    new_path: str


class Sunset(_Strict):
    """Deprecation notice: successful responses carry Sunset and Deprecation headers."""

    type: Literal["sunset"] = "sunset"
    endpoint_id: str
    date: str = "Thu, 31 Dec 2026 23:59:59 GMT"
    successor: str | None = None


class EndlessPagination(_Strict):
    """Behavioral drift: every page looks like the first, so pagination never terminates."""

    type: Literal["endless_pagination"] = "endless_pagination"
    endpoint_id: str


Mutation = Annotated[
    Union[
        RenameField,
        RemoveField,
        RetypeField,
        SetField,
        WrapItems,
        StatusResponse,
        NonJson,
        RequireHeader,
        PathMoved,
        Sunset,
        EndlessPagination,
    ],
    Field(discriminator="type"),
]


class DriftScenario(_Strict):
    name: str = "drift"
    mutations: list[Mutation] = Field(default_factory=list)

    def for_endpoint(self, endpoint_id: str) -> list[Any]:
        return [m for m in self.mutations if getattr(m, "endpoint_id", None) == endpoint_id]


class DriftWorld:
    """The API as it behaves now: a MockServer for the pinned manifest wrapped in a scenario."""

    def __init__(
        self,
        pinned: IntegrationManifest,
        scenario: DriftScenario | None = None,
        bearer_validator: Callable[[str], bool] | None = None,
        list_size: int = 3,
    ) -> None:
        self.pinned = pinned
        self.scenario = scenario or DriftScenario()
        self.mock = MockServer(pinned, list_size=list_size, bearer_validator=bearer_validator)
        self._moved = [(m, MockServer.compile_path(m.new_path)) for m in self.scenario.mutations if isinstance(m, PathMoved)]
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        request.read()

        # Auth drift applies to every route. When the new credential is present, the request is
        # passed on with the credential the pinned mock still checks for.
        for m in self.scenario.mutations:
            if isinstance(m, RequireHeader):
                sent = request.headers.get(m.name)
                if not sent or (m.value is not None and sent != m.value):
                    return httpx.Response(401, json={"error": f"missing or invalid {m.name} header"})
                request = self._with_legacy_auth(request)

        # A successor path is served as the endpoint it replaced.
        arrived_via_successor: PathMoved | None = None
        for m, pattern in self._moved:
            if pattern.search(request.url.path):
                old_template = self.pinned.endpoint(m.endpoint_id).path
                request = _rebuild(request, path=_translate_path(request.url.path, m.new_path, old_template))
                arrived_via_successor = m
                break

        endpoint = self.mock.match(request)
        if endpoint is None:
            return self.mock.handle(request)
        mutations = self.scenario.for_endpoint(endpoint.id)

        for m in mutations:
            if isinstance(m, PathMoved) and arrived_via_successor is None:
                link = f'<{m.new_path}>; rel="successor-version"'
                body = {"error": f"{endpoint.path} has been retired; use {m.new_path}"}
                return httpx.Response(410, json=body, headers={"Link": link, "Deprecation": "true"})
            if isinstance(m, StatusResponse):
                return httpx.Response(m.status_code, json=m.body, headers=m.headers)
            if isinstance(m, NonJson):
                html = "<html><body><h1>Service temporarily unavailable</h1></body></html>"
                return httpx.Response(200, text=html, headers={"content-type": "text/html"})
            if isinstance(m, EndlessPagination):
                request = _strip_pagination(request, endpoint)

        response = self.mock.handle(request)
        if response.status_code != 200:
            return response

        body = response.json()
        extra_headers: dict[str, str] = {}
        for m in mutations:
            if isinstance(m, RenameField):
                for record in _records(body, endpoint):
                    if m.old in record:
                        record[m.new] = record.pop(m.old)
            elif isinstance(m, RemoveField):
                for record in _records(body, endpoint):
                    record.pop(m.field, None)
            elif isinstance(m, (RetypeField, SetField)):
                for record in _records(body, endpoint):
                    set_path(record, m.field, m.value)
            elif isinstance(m, WrapItems):
                body = _wrap_items(body, endpoint, m.key)
            elif isinstance(m, Sunset):
                extra_headers["Sunset"] = m.date
                extra_headers["Deprecation"] = "true"
                if m.successor:
                    extra_headers["Link"] = f'<{m.successor}>; rel="successor-version"'
        headers = {"content-type": "application/json", **extra_headers}
        return httpx.Response(200, content=json.dumps(body).encode(), headers=headers)

    def _with_legacy_auth(self, request: httpx.Request) -> httpx.Request:
        auth = self.pinned.auth
        headers = dict(request.headers)
        url = request.url
        if isinstance(auth, ApiKeyAuth):
            if auth.location == "header":
                headers[auth.name] = auth.prefix + "legacy"
            else:
                url = url.copy_set_param(auth.name, auth.prefix + "legacy")
        elif isinstance(auth, BasicAuth):
            headers["Authorization"] = "Basic " + base64.b64encode(b"legacy:x").decode("ascii")
        else:
            return request
        return _rebuild(request, url=url, headers=headers)


# --- helpers -------------------------------------------------------------------------------


def _records(body: Any, endpoint: Endpoint) -> list[dict[str, Any]]:
    items = get_path(body, endpoint.items_path) if endpoint.items_path else body
    if isinstance(items, list):
        return [r for r in items if isinstance(r, dict)]
    return [items] if isinstance(items, dict) else []


def _wrap_items(body: Any, endpoint: Endpoint, key: str) -> Any:
    if isinstance(body, list):
        return {key: body}
    if not isinstance(body, dict) or not endpoint.items_path:
        return body
    items = get_path(body, endpoint.items_path)
    if "." not in endpoint.items_path:
        body.pop(endpoint.items_path, None)
    set_path(body, key, items)
    return body


def _translate_path(request_path: str, new_template: str, old_template: str) -> str:
    """Map a concrete successor path back onto the retired template, carrying path variables over."""
    new_segments = new_template.strip("/").split("/")
    request_segments = request_path.strip("/").split("/")
    tail = request_segments[-len(new_segments) :]
    prefix = request_segments[: -len(new_segments)]
    variables = {seg[1:-1]: value for seg, value in zip(new_segments, tail) if seg.startswith("{") and seg.endswith("}")}
    old_segments = [
        variables.get(seg[1:-1], seg) if seg.startswith("{") and seg.endswith("}") else seg
        for seg in old_template.strip("/").split("/")
    ]
    return "/" + "/".join(prefix + old_segments)


def _strip_pagination(request: httpx.Request, endpoint: Endpoint) -> httpx.Request:
    pg = endpoint.pagination
    url = request.url
    for param in (pg.page_param, pg.offset_param, pg.cursor_param):
        if param in url.params:
            url = url.copy_remove_param(param)
    return _rebuild(request, url=url)


def _rebuild(
    request: httpx.Request,
    url: httpx.URL | None = None,
    path: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Request:
    target = url or request.url
    if path is not None:
        target = target.copy_with(path=path)
    merged = dict(request.headers) if headers is None else headers
    merged = {k: v for k, v in merged.items() if k.lower() not in ("host", "content-length")}
    return httpx.Request(request.method, target, headers=merged, content=request.content)


__all__ = [
    "DriftScenario",
    "DriftWorld",
    "Mutation",
    "RenameField",
    "RemoveField",
    "RetypeField",
    "SetField",
    "WrapItems",
    "StatusResponse",
    "NonJson",
    "RequireHeader",
    "PathMoved",
    "Sunset",
    "EndlessPagination",
]
