"""Endpoint clustering: group samples by method and path pattern.

A path segment becomes a variable when it looks like an identifier (digits, a UUID, a long hex
string, a prefixed number such as 'E-1042'), or when three or more different literals appear in
the same position on paths that return the same shape of body. The segments every path shares up
to the API's version marker form the base path, which belongs in the manifest's base_url.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.discovery.models import NOT_JSON, TrafficSample

VAR = "{}"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX = re.compile(r"^[0-9a-f]{12,}$", re.I)
_PREFIXED_NUMBER = re.compile(r"^[A-Za-z]{0,5}[-_]?\d{2,}$")
_OPAQUE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-]{16,}$")
_VERSION = re.compile(r"^(v\d+(\.\d+)*|api|rest)$", re.I)
_MIN_SLUG_VALUES = 3


def looks_like_id(segment: str) -> bool:
    if segment.isdigit() or _UUID.match(segment) or _HEX.match(segment) or _OPAQUE.match(segment):
        return True
    return bool(_PREFIXED_NUMBER.match(segment)) and not _VERSION.match(segment)


@dataclass
class Cluster:
    method: str
    shape: tuple[str, ...]  # path segments including the base path, variables as '{}'
    samples: list[TrafficSample] = field(default_factory=list)
    path: str = ""  # template relative to the base path, variables named
    path_params: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


def cluster_samples(samples: list[TrafficSample]) -> tuple[str, list[Cluster]]:
    """Return the base path and the endpoint clusters, ordered by path then method."""
    groups: dict[tuple[str, tuple[str, ...]], Cluster] = {}
    for sample in samples:
        segments = [seg for seg in sample.path.split("/") if seg]
        shape = tuple(VAR if looks_like_id(seg) else seg for seg in segments)
        groups.setdefault((sample.method, shape), Cluster(sample.method, shape)).samples.append(sample)
    clusters = _merge_slugs(list(groups.values()))
    base = _base_path([c.shape for c in clusters])
    for c in clusters:
        c.path, c.path_params = _template(c.shape[len(base) :])
    clusters.sort(key=lambda c: (c.path.count("{"), c.path, c.method))
    return "/" + "/".join(base) if base else "", clusters


def _merge_slugs(clusters: list[Cluster]) -> list[Cluster]:
    """Literal segments that vary like identifiers: '/users/jsmith', '/users/adoe', '/users/bkim'
    returning the same shape of body are one endpoint with a variable."""
    changed = True
    while changed:
        changed = False
        buckets: dict[tuple[str, int, tuple[str, ...]], list[Cluster]] = {}
        for c in clusters:
            for position, segment in enumerate(c.shape):
                if segment != VAR:
                    rest = c.shape[:position] + (VAR,) + c.shape[position + 1 :]
                    buckets.setdefault((c.method, position, rest), []).append(c)
        for (method, _, merged_shape), members in buckets.items():
            signatures = {_signature(m) for m in members}
            if len(members) >= _MIN_SLUG_VALUES and len(signatures) == 1 and None not in signatures:
                merged = next((c for c in clusters if c.method == method and c.shape == merged_shape), None)
                if merged is None:
                    merged = Cluster(method, merged_shape)
                    clusters.append(merged)
                for m in members:
                    merged.samples.extend(m.samples)
                    clusters.remove(m)
                changed = True
                break
    return clusters


def _signature(cluster: Cluster) -> tuple[Any, ...] | None:
    """Shape of the first successful JSON body: its top-level keys and the keys of the first record
    under each list. Two paths with the same signature are probably the same endpoint."""
    for sample in cluster.samples:
        if not 200 <= sample.status < 300:
            continue
        body = sample.response_json()
        if body is NOT_JSON:
            continue
        return _shape_of(body, depth=0)
    return None


def _shape_of(value: Any, depth: int) -> tuple[Any, ...]:
    if isinstance(value, dict):
        if depth >= 2:
            return ("object", tuple(sorted(value)))
        return ("object", tuple((k, _shape_of(v, depth + 1)) for k, v in sorted(value.items()) if isinstance(v, (dict, list))), tuple(sorted(value)))
    if isinstance(value, list):
        first = next((v for v in value if isinstance(v, dict)), None)
        return ("list", _shape_of(first, depth + 1) if first is not None else ())
    return ()


def _base_path(shapes: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not shapes:
        return ()
    prefix: list[str] = []
    for column in zip(*shapes):
        if len(set(column)) != 1 or column[0] == VAR:
            break
        prefix.append(column[0])
    # Every endpoint keeps at least one segment of its own.
    while prefix and any(len(shape) <= len(prefix) for shape in shapes):
        prefix.pop()
    markers = [i for i, seg in enumerate(prefix) if _VERSION.match(seg)]
    if markers:
        return tuple(prefix[: markers[-1] + 1])
    if len(shapes) == 1:
        # One endpoint gives no evidence of where the API root ends; keep the path whole.
        return ()
    return tuple(prefix)


def _template(shape: tuple[str, ...]) -> tuple[str, list[str]]:
    names: list[str] = []
    out: list[str] = []
    previous = ""
    for segment in shape:
        if segment == VAR:
            name = f"{singular(previous)}_id" if previous else "id"
            name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            candidate, n = name, 2
            while candidate in names:
                candidate, n = f"{name}_{n}", n + 1
            names.append(candidate)
            out.append("{" + candidate + "}")
        else:
            out.append(segment)
            previous = segment
    return "/" + "/".join(out), names


def singular(word: str) -> str:
    lower = word.lower()
    if lower.endswith("ies") and len(lower) > 4:
        return lower[:-3] + "y"
    if lower.endswith("sses") or lower.endswith("xes"):
        return lower[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return lower[:-1]
    return lower
