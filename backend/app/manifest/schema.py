"""The integration manifest: a declarative description of an API that the generic runtime
interprets. The synthesis agent produces manifests, the verification harness tests them,
the registry versions them, and the repair pipeline patches them.

Keeping integrations as data (not generated code) is the core design decision of the
platform: diffs are small and reviewable, drift maps onto manifest fields, and verification
can be schema-driven.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Any, Iterator, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


from app.canonical.people import CANONICAL_OBJECTS, canonical_fields
from app.runtime.transforms import TRANSFORMS

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

_STATUS = re.compile(r"^[1-5]\d\d$")
_TYPE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_LOCAL_REF = re.compile(r"^#/\$defs/([A-Za-z_][A-Za-z0-9_]*)$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_PATH_VAR = re.compile(r"{([a-zA-Z_][a-zA-Z0-9_]*)}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Auth -----------------------------------------------------------------------------


class NoAuth(_Strict):
    type: Literal["none"] = "none"
class ApiKeyAuth(_Strict): #we're defining a typeset for the auth
    """A static key sent in a header or query parameter."""

    type: Literal["api_key"] = "api_key"
    location: Literal["header", "query"] = "header"
    name: str = Field(description="Header or query parameter name, e.g. 'X-Api-Key'")
    prefix: str = Field(default="", description="Text prepended to the key, e.g. 'Token '")
    secret_ref: str = Field(description="Name of the secret in the connection that holds the key")


class BearerAuth(_Strict): #another auth type 
    type: Literal["bearer"] = "bearer"
    secret_ref: str


class BasicAuth(_Strict):
    """HTTP Basic. Each half is either a secret reference or a literal (some APIs use the
    API key as the username and a throwaway password)."""

    type: Literal["basic"] = "basic"
    username_secret_ref: str | None = None
    username_literal: str | None = None
    password_secret_ref: str | None = None
    password_literal: str | None = None

    @model_validator(mode="after")
    def _one_source_each(self) -> "BasicAuth":
        if (self.username_secret_ref is None) == (self.username_literal is None):
            raise ValueError("basic auth needs exactly one of username_secret_ref / username_literal")
        if (self.password_secret_ref is None) == (self.password_literal is None):
            raise ValueError("basic auth needs exactly one of password_secret_ref / password_literal")
        return self


class OAuth2Auth(_Strict):
    """OAuth 2.0 authorization code flow. The platform registers one OAuth app per integration;
    each tenant connection holds its own tokens in the vault. The gateway only ever sees the
    access token, exposed through the secret ref 'access_token'."""

    type: Literal["oauth2"] = "oauth2"
    flow: Literal["authorization_code"] = "authorization_code"
    authorization_url: str
    token_url: str
    scopes: list[str] = Field(default_factory=list, description="Scopes needed by every endpoint")
    scope_separator: str = " "
    pkce: bool = True
    token_auth_method: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post"
    refresh_leeway_seconds: int = Field(default=300, ge=0, description="Refresh this many seconds before expiry")
    extra_authorization_params: dict[str, str] = Field(default_factory=dict)


Auth = Annotated[Union[NoAuth, ApiKeyAuth, BearerAuth, BasicAuth, OAuth2Auth], Field(discriminator="type")]


# --- Endpoints -------------------------------------------------------------------------


Direction = Literal["called", "served"]
class Parameter(_Strict):
    name: str
    location: Literal["path", "query", "header"]
    required: bool = False
    description: str = ""

class ResponseHeader(_Strict):
    name: str
    required: bool = False
    description: str = ""


class Pagination(_Strict):
    style: Literal["none", "page", "offset", "cursor"] = "none"
    page_param: str = "page"
    size_param: str | None = None
    page_size: int = 100
    offset_param: str = "offset"
    limit_param: str = "limit"
    cursor_param: str = "cursor"
    next_cursor_path: str | None = Field(default=None, description="Dotted path in the body to the next cursor")


class Endpoint(_Strict):
    id: str = Field(description="Stable identifier, e.g. 'list_employees'")
    method: HttpMethod
    path: str = Field(description="Path relative to base_url; may contain {path_params}")
    description: str = ""
    parameters: list[Parameter] = Field(default_factory=list)
    default_query: dict[str, str] = Field(default_factory=dict)
    default_headers: dict[str, str] = Field(default_factory=dict)
    response_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for the full response body; every live response is validated against it",
    )
    items_path: str | None = Field(
        default=None,
        description="Dotted path to the list of records in the body. None means the body itself is one record (or a list).",
    )
    pagination: Pagination = Field(default_factory=Pagination)
    scopes: list[str] = Field(default_factory=list, description="OAuth scopes this endpoint needs beyond the base scopes")
    request_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for the request body of a write endpoint; a body is validated before it is sent",
    )
    idempotency_header: str | None = Field(
        default=None,
        description="Header the API accepts as an idempotency key. When set, a failed write can be retried safely.",
    )
    direction: Direction = Field(
        default="called",
        description="'called': the platform is the client of this endpoint. " \
        "'served': the platform is the server of this endpoint." \
        "this proxy endpoint is inlined into the platform to observe APIs traffic and dataflows"

    )
    responses: dict[str, dict[str, Any]] = Field(
        default_factory = dict, 
        description="JSON Schema per status code for bodies other than the success body" \
        "For ex: {'404': {...}  }"
    )
    response_headers: list[ResponseHeader] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _status_keys(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
          for code in v:
              if not _STATUS.match(code):
                  raise ValueError(f"responses key '{code}' is not a three-digit HTTP status code")
          return v
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("endpoint id must be a lowercase slug")
        return v

    @model_validator(mode="after")
    def _path_params_declared(self) -> "Endpoint":
        declared = {p.name for p in self.parameters if p.location == "path"}
        for var in _PATH_VAR.findall(self.path):
            if var not in declared:
                raise ValueError(f"path variable '{var}' is not declared as a path parameter")
        return self


# --- Mappings --------------------------------------------------------------------------


class FieldMap(_Strict):
    target: str = Field(description="Canonical field name")
    source: str | None = Field(default=None, description="Dotted path inside one raw record")
    transform: str | None = Field(default=None, description="Name of a registered transform")
    args: dict[str, Any] = Field(default_factory=dict, description="Transform arguments")

    @field_validator("transform")
    @classmethod
    def _known_transform(cls, v: str | None) -> str | None:
        if v is not None and v not in TRANSFORMS:
            raise ValueError(f"unknown transform '{v}'; known: {sorted(TRANSFORMS)}")
        return v

    @model_validator(mode="after")
    def _source_or_transform(self) -> "FieldMap":
        if self.source is None and self.transform is None:
            raise ValueError(f"field map for '{self.target}' needs a source path or a transform")
        return self


class Mapping(_Strict):
    endpoint_id: str
    canonical_object: str
    fields: list[FieldMap]

    @field_validator("canonical_object")
    @classmethod
    def _known_object(cls, v: str) -> str:
        if v not in CANONICAL_OBJECTS:
            raise ValueError(f"unknown canonical object '{v}'; known: {sorted(CANONICAL_OBJECTS)}")
        return v

    @model_validator(mode="after")
    def _targets_exist(self) -> "Mapping":
        allowed = canonical_fields(self.canonical_object)
        seen: set[str] = set()
        for f in self.fields:
            if f.target not in allowed:
                raise ValueError(
                    f"'{f.target}' is not a field of {self.canonical_object}; allowed: {sorted(allowed)}"
                )
            if f.target in seen:
                raise ValueError(f"'{f.target}' is mapped twice")
            seen.add(f.target)
        if "source_id" not in seen:
            raise ValueError(f"mapping for {self.canonical_object} must map source_id")
        return self


class RequestFieldMap(_Strict):
    """One field of a request body, filled from a canonical record: the inverse of FieldMap."""

    target: str = Field(description="Dotted path inside the request body")
    source: str | None = Field(default=None, description="Canonical field name")
    transform: str | None = Field(default=None, description="Name of a registered transform")
    args: dict[str, Any] = Field(default_factory=dict, description="Transform arguments; paths refer to canonical fields")

    @field_validator("transform")
    @classmethod
    def _known_transform(cls, v: str | None) -> str | None:
        if v is not None and v not in TRANSFORMS:
            raise ValueError(f"unknown transform '{v}'; known: {sorted(TRANSFORMS)}")
        return v

    @model_validator(mode="after")
    def _source_or_transform(self) -> "RequestFieldMap":
        if self.source is None and self.transform is None:
            raise ValueError(f"request field '{self.target}' needs a canonical source or a transform")
        return self


class RequestMapping(_Strict):
    """How a canonical object becomes the request body of a write endpoint."""

    endpoint_id: str
    canonical_object: str
    fields: list[RequestFieldMap]
    omit_null: bool = Field(default=True, description="Leave a field out of the body when its value is null")
    id_param: str | None = Field(
        default=None,
        description="Path parameter that carries the destination's own record id (update endpoints)",
    )
    response_id_path: str | None = Field(
        default=None,
        description="Dotted path in the response body to the id the destination assigned to the record",
    )

    @field_validator("canonical_object")
    @classmethod
    def _known_object(cls, v: str) -> str:
        if v not in CANONICAL_OBJECTS:
            raise ValueError(f"unknown canonical object '{v}'; known: {sorted(CANONICAL_OBJECTS)}")
        return v

    @model_validator(mode="after")
    def _sources_exist(self) -> "RequestMapping":
        allowed = canonical_fields(self.canonical_object) | {"source_integration"}
        seen: set[str] = set()
        for f in self.fields:
            if f.source is not None and f.source not in allowed:
                raise ValueError(f"'{f.source}' is not a field of {self.canonical_object}; allowed: {sorted(allowed)}")
            if f.target in seen:
                raise ValueError(f"request field '{f.target}' is filled twice")
            seen.add(f.target)
        if not self.fields:
            raise ValueError("a request mapping needs at least one field")
        return self


# --- Policies --------------------------------------------------------------------------


class RateLimit(_Strict):
    requests_per_second: float = Field(default=5.0, gt=0)
    burst: int = Field(default=10, ge=1)


class RetryPolicy(_Strict):
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: float = Field(default=0.5, ge=0)
    retry_on_status: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])


# --- Types -----------------------------------------------------------------------------

def iter_refs(schema: Any) -> Iterator[str]:
    ''' Every $ref string anywhere inside a JSON schema '''
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            yield ref 
        for value in schema.values():
            yield from iter_refs(value)
    elif isinstance(schema, list):
        for val in schema:
            yield from iter_refs(val)

def inline_types(schema: Any, types: dicts[str, dict[str, Any]], _stack: tuple[str, ...] = ()) -> Any:
    ''' Now, we replace #/$defs/Name references with the type's named schema
        Basically, any keywords written next to a $ref will be replaced with the actual type definition.
        This function will recursively inline the types into the schema.
        Func returns a modified schema with all $ref references inlined, allowing us to work with a self-contained schema without external references.
        We're still 
    '''
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            match = _LOCAL_REF.match(ref)
            if match and match.group(1) in types and match.group(1) not in _stack: #checking stack so I'm not inlining the same type recursively
                name = match.group(1) 
                expanded = inline_types(types[name], types, _stack + (name,))
                siblings = {k: v for k, v in schema.items() if k != "$ref"}
                return {**expanded, **siblings}
            return dict(schema)
        return {k: inline_types(v, types, _stack) for k, v in schema.items()}
        # If no $ref is found or it can't be inlined, process the rest of the schema
    if isinstance(schema, list): #this is different from the above if isinstance (ref, str) because we are handling a list of schemas
                                 #a list of schemas is different than the individual ref, str because each element in the list needs to be processed separately
                                 # we could expect schema as both a list OR a dict because JSON schema allows both structures for defining types and their references.
        return [inline_types(val, types, _stack) for val in schema]
    return schema
# --- Manifest --------------------------------------------------------------------------


class IntegrationManifest(_Strict):
    name: str = Field(description="Slug used everywhere as the integration identifier")
    version: str = Field(default="0.1.0", pattern=r"^\d+\.\d+\.\d+$")
    display_name: str
    description: str = ""
    base_url: str = Field(description="May contain {config_vars} filled from the connection, e.g. a tenant subdomain")
    config_vars: list[str] = Field(default_factory=list, description="Connection config keys required to fill base_url")
    auth: Auth
    default_headers: dict[str, str] = Field(default_factory=dict)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    endpoints: list[Endpoint]
    mappings: list[Mapping] = Field(default_factory=list)
    request_mappings: list[RequestMapping] = Field(
        default_factory=list, description="Write endpoints: canonical object to request body"
    )
    types: dict[str, dict[str, Any]] = Field(default_factory=dict, description="Custom types used in the manifest; we use {'$ref': '#/$defs/Name'} to reference them")


    @field_validator("name")
    @classmethod

    def _type_names(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]: # a quick typechecking function
        for name, schema in v.items():
            if not _TYPE_NAME.match(name):
                raise ValueError(f"type name '{name}' must be an identifier")
            if not isinstance(schema, dict):
                raise ValueError(f"type '{name}' must be a JSON schema typa object")
        return v
    
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("name must be a lowercase slug")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "IntegrationManifest":
        ids = [e.id for e in self.endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("endpoint ids must be unique")
        if not ids:
            raise ValueError("manifest needs at least one endpoint")
        for m in self.mappings:
            if m.endpoint_id not in ids:
                raise ValueError(f"mapping refers to unknown endpoint '{m.endpoint_id}'")
        read_mapped = {m.endpoint_id for m in self.mappings}
        written: set[str] = set()
        for rm in self.request_mappings:
            if rm.endpoint_id not in ids:
                raise ValueError(f"request mapping refers to unknown endpoint '{rm.endpoint_id}'")
            if rm.endpoint_id in written:
                raise ValueError(f"endpoint '{rm.endpoint_id}' has two request mappings")
            written.add(rm.endpoint_id)
            endpoint = self.endpoint(rm.endpoint_id)
            if endpoint.method not in ("POST", "PUT", "PATCH"):
                raise ValueError(f"request mapping on '{rm.endpoint_id}' needs a POST, PUT or PATCH endpoint")
            if rm.endpoint_id in read_mapped:
                raise ValueError(f"endpoint '{rm.endpoint_id}' cannot carry both a response mapping and a request mapping")
            path_params = {p.name for p in endpoint.parameters if p.location == "path"}
            if rm.id_param is not None and rm.id_param not in path_params:
                raise ValueError(f"id_param '{rm.id_param}' is not a path parameter of '{rm.endpoint_id}'")
        declared = set(self.config_vars)
        for var in _PATH_VAR.findall(self.base_url):
            if var not in declared:
                raise ValueError(f"base_url variable '{var}' must be listed in config_vars")
        for owner, schema in self._schemas():
              for ref in iter_refs(schema):
                  match = _LOCAL_REF.match(ref)
                  if match is None:
                      raise ValueError(f"{owner}: only local type references'#/$defs/<Name>' are allowed, got '{ref}'")
                  if match.group(1) not in self.types:
                      raise ValueError(f"{owner}: reference to unknown type'{match.group(1)}'; known: {sorted(self.types)}")
        return self

    def _schemas(self) -> Iterator[tuple[str, dict[str, Any]]]:
          """Every JSON Schema in the manifest, with a label for error messages."""
          for name, schema in self.types.items():
              yield f"type '{name}'", schema
          for e in self.endpoints:
              if e.response_schema is not None:
                  yield f"endpoint '{e.id}' response_schema", e.response_schema
              if e.request_schema is not None:
                  yield f"endpoint '{e.id}' request_schema", e.request_schema
              for code, schema in e.responses.items():
                  yield f"endpoint '{e.id}' responses[{code}]", schema

    def resolve(self, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        """A schema with every type reference inlined, ready for a validator or the mock.

        Recursive types cannot be inlined completely; the references that remain are made
        resolvable by attaching the manifest's types as $defs."""
        if schema is None:
            return None
        if not self.types:
            return schema
        resolved = inline_types(schema, self.types)
        if any(True for _ in iter_refs(resolved)):
            resolved = {**resolved, "$defs": {**self.types, **resolved.get("$defs", {})}}
        return resolved

    def body_schema(self, endpoint: Endpoint, status: int) -> dict[str, Any] | None:
        """The schema a body with this status should match: a declared shape for that status, or the
        success schema for a 2xx. None means nothing is declared for it."""
        declared = endpoint.responses.get(str(status))
        if declared is not None:
            return self.resolve(declared)
        if 200 <= status < 300:
            return self.resolve(endpoint.response_schema)
        return None

    def served_endpoints(self) -> list[Endpoint]:
        return [e for e in self.endpoints if e.direction == "served"]

    def endpoint(self, endpoint_id: str) -> Endpoint:
        for e in self.endpoints:
            if e.id == endpoint_id:
                return e
        raise KeyError(f"unknown endpoint '{endpoint_id}'")

    def mapping_for(self, endpoint_id: str) -> Mapping | None:
        return next((m for m in self.mappings if m.endpoint_id == endpoint_id), None)

    def request_mapping_for(self, endpoint_id: str) -> RequestMapping | None:
        return next((m for m in self.request_mappings if m.endpoint_id == endpoint_id), None)

    def secret_refs(self) -> list[str]:
        a = self.auth
        if isinstance(a, (ApiKeyAuth, BearerAuth)):
            return [a.secret_ref]
        if isinstance(a, BasicAuth):
            return [r for r in (a.username_secret_ref, a.password_secret_ref) if r]
        if isinstance(a, OAuth2Auth):
            return ["access_token"]
        return []

    def required_scopes(self, endpoint_ids: list[str] | None = None) -> list[str]:
        """Minimal scope set: base scopes plus the scopes of the endpoints in use (all by default)."""
        if not isinstance(self.auth, OAuth2Auth):
            return []
        scopes: list[str] = list(self.auth.scopes)
        for e in self.endpoints:
            if endpoint_ids is None or e.id in endpoint_ids:
                scopes.extend(s for s in e.scopes if s not in scopes)
        return scopes


def load_manifest(data: dict[str, Any]) -> IntegrationManifest:
    return IntegrationManifest.model_validate(data)


def load_manifest_file(path: str) -> IntegrationManifest:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    return load_manifest(data)
