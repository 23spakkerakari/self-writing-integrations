"""Schema inference: merge the bodies seen on one endpoint into a JSON Schema and field statistics.

Inference is deliberately lenient while evidence is thin. With fewer than MIN_STRICT_RECORDS
records, a field that was always present is still not marked required and a field that was never
null is still allowed to be null, because a schema stricter than the API would raise drift alarms
the first time an optional field is absent. The exceptions are the fields a mapping depends on,
which the builder passes in as strict paths.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable

from app.discovery.cluster import Cluster
from app.discovery.models import NOT_JSON, FieldStat, InferredEndpoint, QueryParam
from app.runtime.transforms import TRANSFORMS

MIN_STRICT_RECORDS = 5
_MAX_EXAMPLES = 3
_MAX_DISTINCT = 60

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_URI = re.compile(r"^https?://\S+$")

_PAGE_PARAMS = {"page", "page_number", "pagenumber", "p"}
_SIZE_PARAMS = {"per_page", "per", "page_size", "pagesize", "limit", "size", "count", "top"}
_OFFSET_PARAMS = {"offset", "skip", "start"}
_CURSOR_PARAMS = {"cursor", "after", "starting_after", "page_token", "next_token", "continuation", "continuation_token"}
_CURSOR_KEYS = ("next_cursor", "nextCursor", "next_page_token", "nextPageToken", "next", "cursor", "continuation_token")
_CURSOR_PARENTS = ("", "meta", "paging", "pagination", "links", "page_info")

RecordScorer = Callable[[list[str]], float]


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    return "array"


class Node:
    """Everything observed at one position in a body, across samples."""

    __slots__ = ("seen", "types", "objects", "props", "items", "examples", "distinct", "overflow")

    def __init__(self) -> None:
        self.seen = 0
        self.types: Counter[str] = Counter()
        self.objects = 0
        self.props: dict[str, Node] = {}
        self.items: Node | None = None
        self.examples: list[Any] = []
        self.distinct: set[Any] = set()
        self.overflow = False

    def observe(self, value: Any) -> None:
        self.seen += 1
        kind = json_type(value)
        self.types[kind] += 1
        if kind == "object":
            self.objects += 1
            for key, child in value.items():
                self.props.setdefault(key, Node()).observe(child)
        elif kind == "array":
            if self.items is None:
                self.items = Node()
            for element in value:
                self.items.observe(element)
        elif kind != "null":
            if len(self.distinct) < _MAX_DISTINCT:
                self.distinct.add(value)
            else:
                self.overflow = True
            if value not in self.examples and len(self.examples) < _MAX_EXAMPLES:
                self.examples.append(value if not isinstance(value, str) else value[:120])

    def at(self, path: str | None) -> "Node | None":
        node: Node | None = self
        for part in [p for p in (path or "").split(".") if p]:
            if node is None:
                return None
            node = node.props.get(part)
        return node

    def value_types(self) -> list[str]:
        kinds = set(self.types) - {"null"}
        if {"integer", "number"} <= kinds:
            kinds.discard("integer")
        return sorted(kinds)

    def string_format(self) -> str | None:
        strings = [v for v in self.distinct if isinstance(v, str)]
        if not strings or self.value_types() != ["string"]:
            return None
        for name, pattern in (("email", _EMAIL), ("date", _DATE), ("date-time", _DATETIME), ("uri", _URI)):
            if all(pattern.match(v) for v in strings):
                return name
        return None


def to_schema(node: Node, lenient: bool, strict_paths: frozenset[str] = frozenset(), path: str = "") -> dict[str, Any]:
    """JSON Schema for a node. `strict_paths` are body-relative paths ('employees[].id') that stay
    required and non-null whatever the evidence, because a mapping reads them."""
    kinds = node.value_types()
    strict = path in strict_paths
    nullable = "null" in node.types or (lenient and not strict and kinds not in (["object"], ["array"]))
    if not kinds:
        return {}
    if kinds == ["object"]:
        schema: dict[str, Any] = {"type": "object"}
        if node.props:
            schema["properties"] = {
                key: to_schema(child, lenient, strict_paths, f"{path}.{key}".lstrip(".")) for key, child in node.props.items()
            }
            required = [
                key
                for key, child in node.props.items()
                if child.seen == node.objects and (not lenient or f"{path}.{key}".lstrip(".") in strict_paths)
            ]
            if required:
                schema["required"] = required
    elif kinds == ["array"]:
        schema = {"type": "array"}
        if node.items is not None and node.items.seen:
            schema["items"] = to_schema(node.items, lenient, strict_paths, path + "[]")
    else:
        schema = {"type": kinds[0] if len(kinds) == 1 else kinds}
        fmt = node.string_format()
        if fmt:
            schema["format"] = fmt
    if nullable:
        declared = schema["type"]
        schema["type"] = [*declared, "null"] if isinstance(declared, list) else [declared, "null"]
    return schema


def field_stats(record: Node, prefix: str = "") -> list[FieldStat]:
    """One FieldStat per leaf field of a record, descending into objects and lists of objects."""
    out: list[FieldStat] = []
    for key, child in record.props.items():
        path = f"{prefix}{key}"
        kinds = child.value_types()
        if kinds == ["object"] and child.props:
            out.extend(field_stats(child, path + "."))
            continue
        if kinds == ["array"] and child.items is not None and child.items.value_types() == ["object"]:
            out.extend(field_stats(child.items, path + "[]."))
            continue
        total = record.objects
        confidence = 1 - 0.5 ** max(total, 0)
        if len(kinds) > 1:
            confidence *= 0.8
        distinct = len(child.distinct)
        repeats = sum(child.types[k] for k in ("string", "integer", "number", "boolean"))
        enum = None
        if kinds == ["string"] and not child.overflow and distinct <= 8 and repeats >= max(2 * distinct, 3):
            enum = sorted(child.distinct, key=str)
        out.append(
            FieldStat(
                path=path,
                types=kinds,
                nullable="null" in child.types,
                present=child.seen,
                total=total,
                distinct=distinct,
                format=child.string_format(),
                enum=enum,
                examples=child.examples,
                confidence=round(confidence, 2),
            )
        )
    return out


def find_records(body: Any, scorer: RecordScorer | None = None) -> tuple[str | None, bool]:
    """Where the records are in a body: (items_path, is_list). A top-level list is its own record
    list; otherwise the best list of objects one or two levels down; otherwise a single wrapped
    object; otherwise the body is the record."""
    if isinstance(body, list):
        return None, True
    if not isinstance(body, dict):
        return None, False
    scalars = [k for k, v in body.items() if not isinstance(v, (dict, list))]
    lists: list[tuple[float, str, float]] = []
    for key, value in body.items():
        for path, candidate in ((key, value), *(((f"{key}.{k}", v) for k, v in value.items()) if isinstance(value, dict) else ())):
            if isinstance(candidate, list) and candidate and all(isinstance(x, dict) for x in candidate):
                keys = sorted({k for x in candidate for k in x})
                affinity = scorer(keys) if scorer else 0.0
                lists.append((affinity * 100 + len(keys) - path.count("."), path, affinity))
            elif isinstance(candidate, list) and not candidate:
                lists.append((-1.0, path, 0.0))
    if lists:
        _, path, affinity = max(lists)
        # A record that carries a list of its own (an employee with jobs) is still one record: its
        # own scalar fields look at least as much like the canonical object as the nested list does.
        own = scorer(scalars) if scorer else 0.0
        is_record = len(scalars) >= 2 and (own >= affinity and own > 0 if scorer else len(scalars) >= 3)
        if not is_record:
            return path, True
        return None, False
    wrappers = [k for k, v in body.items() if isinstance(v, dict) and v]
    if len(wrappers) == 1 and not scalars:
        return wrappers[0], False
    return None, False


def infer_endpoint(cluster: Cluster, scorer: RecordScorer | None = None, strict_fields: frozenset[str] = frozenset()) -> InferredEndpoint:
    """Describe one endpoint from its samples. `strict_fields` are record-relative paths that a
    confirmed mapping reads; they stay required and non-null in the schema."""
    endpoint = InferredEndpoint(key=cluster.key, method=cluster.method, path=cluster.path, path_params=cluster.path_params)
    endpoint.samples = len(cluster.samples)
    endpoint.statuses = dict(Counter(str(s.status) for s in cluster.samples))
    bodies = [b for b in (s.response_json() for s in cluster.samples if 200 <= s.status < 300) if b is not NOT_JSON]
    endpoint.query_params = _query_params(cluster)

    if bodies:
        votes = Counter(find_records(body, scorer) for body in bodies if body not in ([], {}))
        (items_path, is_list) = votes.most_common(1)[0][0] if votes else (None, isinstance(bodies[0], list))
        endpoint.items_path, endpoint.is_list = items_path, is_list
        root = Node()
        for body in bodies:
            root.observe(body)
        record = root.at(items_path)
        if record is not None and is_list:
            record = record.items
        if record is not None and record.objects:
            endpoint.records = record.objects
            endpoint.fields = field_stats(record)
        lenient = endpoint.records < MIN_STRICT_RECORDS
        record_prefix = (items_path or "") + ("[]" if is_list else "")
        strict = {f"{record_prefix}.{f}".lstrip(".") if record_prefix else f for f in strict_fields}
        if items_path:
            parts = items_path.split(".")
            strict.update(".".join(parts[: i + 1]) for i in range(len(parts)))
        endpoint.response_schema = to_schema(root, lenient, frozenset(strict))
        endpoint.pagination = _pagination(endpoint, bodies)
        endpoint.confidence = round(1 - 0.5 ** max(endpoint.records, len(bodies)), 2)
        if lenient and endpoint.records:
            endpoint.notes.append(
                f"{endpoint.records} record(s) seen; with fewer than {MIN_STRICT_RECORDS} the schema keeps every field optional and nullable"
            )
        if not endpoint.records and is_list:
            endpoint.notes.append("the record list was empty in every sample; capture a response with data")
    elif cluster.method == "GET":
        endpoint.notes.append("no successful JSON response captured; the response shape is unknown")

    if cluster.method in ("POST", "PUT", "PATCH"):
        sent = [b for b in (s.request_json() for s in cluster.samples if 200 <= s.status < 300) if isinstance(b, dict)]
        if sent:
            request_root = Node()
            for body in sent:
                request_root.observe(body)
            endpoint.request_fields = field_stats(request_root)
            # A request schema only names what was seen being accepted; nothing is required from one or two posts.
            endpoint.request_schema = to_schema(request_root, lenient=len(sent) < MIN_STRICT_RECORDS)
        else:
            endpoint.notes.append("no accepted JSON request body captured; the request shape is unknown")

    for role in {q.role for q in endpoint.query_params}:
        if role == "auth":
            endpoint.notes.append("a credential travels in the query string")
    return endpoint


def _query_params(cluster: Cluster) -> list[QueryParam]:
    seen: dict[str, list[str]] = {}
    for sample in cluster.samples:
        for name, value in sample.query.items():
            values = seen.setdefault(name, [])
            if value not in values:
                values.append(value)
    out: list[QueryParam] = []
    for name, values in seen.items():
        carried = sum(1 for s in cluster.samples if name in s.query)
        lower = name.lower()
        if values == ["[redacted]"]:
            role = "auth"
        elif lower in _PAGE_PARAMS | _SIZE_PARAMS | _OFFSET_PARAMS | _CURSOR_PARAMS:
            role = "pagination"
        elif len(values) == 1 and carried == len(cluster.samples) and len(cluster.samples) >= 2:
            role = "default"
        else:
            role = "parameter"
        out.append(QueryParam(name=name, values=values[:5], constant=len(values) == 1, role=role))  # type: ignore[arg-type]
    return out


def _pagination(endpoint: InferredEndpoint, bodies: list[Any]) -> dict[str, Any] | None:
    if not endpoint.is_list:
        return None
    names = {q.name.lower(): q for q in endpoint.query_params if q.role == "pagination"}
    size = next((names[n] for n in names if n in _SIZE_PARAMS), None)
    page_size = max((len(_records_of(b, endpoint.items_path)) for b in bodies), default=0) or 100
    if size is not None and size.values and size.values[0].isdigit():
        page_size = int(size.values[0])
    offset = next((names[n] for n in names if n in _OFFSET_PARAMS), None)
    if offset is not None:
        out: dict[str, Any] = {"style": "offset", "offset_param": offset.name, "page_size": page_size}
        if size is not None:
            out["limit_param"] = size.name
        return out
    page = next((names[n] for n in names if n in _PAGE_PARAMS), None)
    if page is not None:
        out = {"style": "page", "page_param": page.name, "page_size": page_size}
        if size is not None:
            out["size_param"] = size.name
        return out
    cursor_path = _cursor_path(bodies)
    cursor = next((names[n] for n in names if n in _CURSOR_PARAMS), None)
    if cursor is not None or cursor_path:
        out = {"style": "cursor", "cursor_param": cursor.name if cursor is not None else "cursor", "page_size": page_size}
        if cursor_path:
            out["next_cursor_path"] = cursor_path
        if size is not None:
            out["size_param"] = size.name
        return out
    return None


def _cursor_path(bodies: list[Any]) -> str | None:
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for parent in _CURSOR_PARENTS:
            holder = body.get(parent) if parent else body
            if isinstance(holder, dict):
                for key in _CURSOR_KEYS:
                    if key in holder and not isinstance(holder[key], (dict, list)):
                        return f"{parent}.{key}".lstrip(".")
    return None


def _records_of(body: Any, items_path: str | None) -> list[Any]:
    value = body
    for part in [p for p in (items_path or "").split(".") if p]:
        value = value.get(part) if isinstance(value, dict) else None
    return value if isinstance(value, list) else []


def parses_as_date(value: Any) -> bool:
    try:
        return TRANSFORMS["to_date"](value, {}, {}) is not None
    except ValueError:
        return False
