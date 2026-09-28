"""A mock API generated from a manifest.

The mock serves every endpoint in the manifest with bodies generated from the endpoint's
response schema, enforces that auth was sent, and simulates pagination. It runs in-process
as an httpx transport, so verification needs no ports and no network.

A single `hook` lets callers mutate the generated body or substitute a response for a given
endpoint. The drift and repair pipeline (milestone three) uses that to simulate every drift
type against an integration before letting the repair agent loose on a real one.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

import httpx

from app.manifest.schema import ApiKeyAuth, BasicAuth, BearerAuth, Endpoint, IntegrationManifest, OAuth2Auth
from app.runtime.paths import get_path, set_path

Hook = Callable[[str, httpx.Request, Any], Any]
# (endpoint_id, request, generated_body) -> body | httpx.Response


class SchemaExampleGenerator:
    """Deterministic, name-aware example generation from a JSON Schema."""

    FIRST = ["Ada", "Grace", "Linus", "Margaret", "Dennis"]
    LAST = ["Lovelace", "Hopper", "Torvalds", "Hamilton", "Ritchie"]

    def __init__(self, root: dict[str, Any], list_size: int = 3) -> None:
        self.root = root
        self.list_size = list_size

    def generate(self, schema: dict[str, Any] | None = None, name: str = "", index: int = 0, depth: int = 0) -> Any:
        schema = self.root if schema is None else schema
        if depth > 12:
            return None
        schema = self._resolve(schema)
        for key in ("example", "const", "default"):
            if key in schema:
                return copy.deepcopy(schema[key])
        if "examples" in schema and schema["examples"]:
            return copy.deepcopy(schema["examples"][0])
        if "enum" in schema and schema["enum"]:
            return schema["enum"][index % len(schema["enum"])]
        for combinator in ("oneOf", "anyOf"):
            if combinator in schema and schema[combinator]:
                return self.generate(schema[combinator][0], name, index, depth + 1)
        if "allOf" in schema:
            merged: dict[str, Any] = {}
            for part in schema["allOf"]:
                part = self._resolve(part)
                merged.setdefault("properties", {}).update(part.get("properties", {}))
                merged.setdefault("required", []).extend(part.get("required", []))
                merged.setdefault("type", part.get("type", "object"))
            return self.generate(merged, name, index, depth + 1)

        typ = schema.get("type")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), "null")
        if typ is None:
            typ = "object" if "properties" in schema else ("array" if "items" in schema else "string")

        if typ == "object":
            out: dict[str, Any] = {}
            for prop, sub in schema.get("properties", {}).items():
                out[prop] = self.generate(sub, prop, index, depth + 1)
            return out
        if typ == "array":
            items = schema.get("items", {"type": "string"})
            size = schema.get("minItems", self.list_size)
            size = max(size, 1)
            return [self.generate(items, name, i, depth + 1) for i in range(size)]
        if typ == "integer":
            return self._integer(schema, name, index)
        if typ == "number":
            return float(self._integer(schema, name, index))
        if typ == "boolean":
            return index % 2 == 0
        if typ == "null":
            return None
        return self._string(schema, name, index)

    # --- helpers ---------------------------------------------------------------------

    def _resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref")
        if not ref or not ref.startswith("#/"):
            return schema
        target: Any = self.root
        for part in ref[2:].split("/"):
            target = target[part]
        merged = {k: v for k, v in schema.items() if k != "$ref"}
        merged.update(target)
        return merged

    def _integer(self, schema: dict[str, Any], name: str, index: int) -> int:
        lo = schema.get("minimum", 0)
        hi = schema.get("maximum")
        value = 1000 + index if "id" in name.lower() else lo + index + 1
        if hi is not None:
            value = min(value, hi)
        return max(value, lo)

    def _string(self, schema: dict[str, Any], name: str, index: int) -> str:
        fmt = schema.get("format", "")
        n = name.lower()
        first, last = self.FIRST[index % 5], self.LAST[index % 5]
        if fmt == "email" or "email" in n:
            return f"{first.lower()}.{last.lower()}@example.com"
        if fmt == "date" or n.endswith("date") or n.startswith("date"):
            return f"2020-0{1 + index % 9}-15"
        if fmt == "date-time" or n.endswith("at") or "timestamp" in n:
            return f"2020-0{1 + index % 9}-15T09:30:00Z"
        if fmt in ("uri", "url") or n.endswith("url") or n.endswith("uri"):
            return f"https://example.com/{n or 'resource'}/{index}"
        if n == "id" or n.endswith("id") or n.endswith("_id"):
            return str(1000 + index)
        if "firstname" in n or n == "first_name" or n == "givenname":
            return first
        if "lastname" in n or n == "last_name" or n == "surname" or n == "familyname":
            return last
        if "displayname" in n or n == "name" or n == "fullname" or n == "full_name":
            return f"{first} {last}"
        if "phone" in n:
            return f"+1-555-01{index:02d}"
        if "status" in n:
            return "Active"
        if "title" in n:
            return ["Engineer", "Manager", "Analyst", "Designer", "Director"][index % 5]
        if "department" in n:
            return ["Engineering", "Product", "Finance", "Design", "Operations"][index % 5]
        if "location" in n or "city" in n:
            return ["London", "Berlin", "Austin", "Toronto", "Sydney"][index % 5]
        if "supervisor" in n or "manager" in n:
            return f"{self.FIRST[(index + 1) % 5]} {self.LAST[(index + 1) % 5]}"
        return f"{name or 'value'}-{index}"


class MockServer:
    def __init__(
        self,
        manifest: IntegrationManifest,
        hook: Hook | None = None,
        list_size: int = 3,
        bearer_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.manifest = manifest
        self.hook = hook
        self.list_size = list_size
        self.bearer_validator = bearer_validator
        self.requests: list[httpx.Request] = []
        # Literal routes first so '/employees/directory' beats '/employees/{id}'.
        ordered = sorted(manifest.endpoints, key=lambda e: e.path.count("{"))
        self._routes = [(e, self._compile(e.path)) for e in ordered]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _compile(path: str) -> re.Pattern[str]:
        pattern = re.sub(r"{[^/]+}", r"[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
        return re.compile(pattern.rstrip("/") + "/?$")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = self._match(request)
        if endpoint is None:
            return httpx.Response(404, json={"error": f"no mock route for {request.method} {request.url.path}"})
        if not self._authorized(request):
            return httpx.Response(401, json={"error": "missing or malformed credentials"})
        body = self._body(endpoint, request)
        if self.hook is not None:
            replaced = self.hook(endpoint.id, request, body)
            if isinstance(replaced, httpx.Response):
                return replaced
            body = replaced
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    def _match(self, request: httpx.Request) -> Endpoint | None:
        for endpoint, pattern in self._routes:
            if endpoint.method == request.method and pattern.search(request.url.path):
                return endpoint
        return None

    def _authorized(self, request: httpx.Request) -> bool:
        auth = self.manifest.auth
        if isinstance(auth, ApiKeyAuth):
            if auth.location == "header":
                return bool(request.headers.get(auth.name))
            return auth.name in request.url.params
        if isinstance(auth, (BearerAuth, OAuth2Auth)):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            token = header[len("Bearer "):]
            return self.bearer_validator(token) if self.bearer_validator else bool(token)
        if isinstance(auth, BasicAuth):
            return request.headers.get("Authorization", "").startswith("Basic ")
        return True

    def _body(self, endpoint: Endpoint, request: httpx.Request) -> Any:
        if endpoint.response_schema is None:
            return {}
        body = SchemaExampleGenerator(endpoint.response_schema, self.list_size).generate()
        pg = endpoint.pagination
        params = request.url.params
        later_page = (
            (pg.style == "page" and params.get(pg.page_param, "1") not in ("1", ""))
            or (pg.style == "offset" and params.get(pg.offset_param, "0") not in ("0", ""))
            or (pg.style == "cursor" and pg.cursor_param in params)
        )
        if endpoint.items_path and isinstance(body, dict):
            if later_page:
                set_path(body, endpoint.items_path, [])
            elif not isinstance(get_path(body, endpoint.items_path), list):
                set_path(body, endpoint.items_path, [])
        elif later_page and isinstance(body, list):
            body = []
        if pg.style == "cursor" and pg.next_cursor_path and isinstance(body, dict):
            set_path(body, pg.next_cursor_path, None if later_page else "mock-cursor-2")
        return body
