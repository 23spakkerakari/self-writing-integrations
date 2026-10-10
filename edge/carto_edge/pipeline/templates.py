"""Template mining with Drain3, per system, persisted (spec 8.2 item 6, 3 "Template", ADR 0016).

Spec 8.2: "Drain3 template mining per system. Each distinct template gets a stable
``template_id`` (hash of system + template text). Parameters become fields ``param_0..n``.
Persist the Drain3 state per system so templates are stable across restarts." ADR 0016 fixes
``template_id = "tpl_" + sha256(system_id + "\\0" + template_text)[:12]``
(:func:`compute_template_id`) and ``file_arrived <generalized file name>`` with digit runs
replaced by ``*`` (:func:`generalize_name`).

Two things are done differently from a stock Drain3 setup, both for spec 2.3:

- **Masking before mining.** Drain3 keeps the first message of a cluster verbatim as the
  template until a second similar message arrives, so ``PO export finished: 412 POs written
  to SHIP_20260923_2112.csv`` would travel to core in clear as a "template". Every
  whitespace token that carries a digit or an ``@`` is replaced by :data:`MASK` before Drain3
  sees it, so numbers, dates, identifiers, file names, addresses and e-mail addresses are
  parameters from the first sighting (invariant 2), and only words that never vary remain in
  the template. Parameters are read back by aligning the template tokens with the original
  tokens (Drain3 preserves the token count), which is exact and linear.
- **Plain-JSON persistence.** Drain3's own snapshots use ``jsonpickle``, which instantiates
  arbitrary classes on load (invariant 8: no pickle). The store serialises the parse tree and
  the clusters itself, one validated JSON file per system under ``EdgeSettings.templates_dir``,
  written atomically (``<system>.json.tmp`` then ``os.replace``), on an interval and on
  :meth:`TemplateStore.flush`/:meth:`TemplateStore.close`. A corrupt or oversized file is
  logged by path and ignored; mining starts fresh.

The registry (:meth:`TemplateStore.registry`) counts records per template with the event kind
and the first and last ``observed_at``; the offline analyzer writes it as ``templates.json``
(``carto_schema.bundle.BundleTemplate``). Memory is bounded by ``max_clusters`` per system
(Drain3's LRU) and the message fed to Drain3 by :data:`MAX_MESSAGE_TOKENS`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from drain3 import TemplateMiner
from drain3.drain import Drain, LogCluster, Node
from drain3.template_miner_config import TemplateMinerConfig

from carto_common.logging import get_logger
from carto_schema.event import (
    MAX_TEMPLATE_TEXT_LEN,
    SOURCE_ID_PATTERN,
    EventKind,
)

__all__ = [
    "MASK",
    "MAX_MESSAGE_TOKENS",
    "MAX_STATE_BYTES",
    "STATE_VERSION",
    "TemplateRecord",
    "TemplateStore",
    "compute_template_id",
    "generalize_name",
]

MASK: Final = "<*>"
"""Drain3's parameter marker; also what masked tokens become before mining."""
MAX_MESSAGE_TOKENS: Final = 512
"""Tokens of one message fed to Drain3; the rest is cut (bounded work per record)."""
MAX_STATE_BYTES: Final = 64 * 1024 * 1024
STATE_VERSION: Final = 1
DEFAULT_MAX_CLUSTERS: Final = 10_000
DEFAULT_FLUSH_INTERVAL: Final = 60.0
DEFAULT_CACHE_SIZE: Final = 8192
_MAX_TREE_DEPTH: Final = 8
_DIGITS: Final = re.compile(r"\d+")
_CHANGED: Final = "cluster_template_changed"


def compute_template_id(system_id: str, template_text: str) -> str:
    """``tpl_`` + the first 12 hex characters of ``sha256(system_id + "\\0" + text)`` (ADR 0016)."""
    digest = hashlib.sha256(f"{system_id}\0{template_text}".encode()).hexdigest()
    return f"tpl_{digest[:12]}"


def generalize_name(file_name: str) -> str:
    """Digit runs become ``*``: ``SHIP_20260923_2112.csv`` -> ``SHIP_*_*.csv`` (ADR 0016)."""
    return _DIGITS.sub("*", file_name)


def _mask_token(token: str) -> bool:
    return "@" in token or any(char.isdigit() for char in token)


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    """One registry entry, the shape of ``carto_schema.bundle.BundleTemplate``."""

    template_id: str
    system_id: str
    template_text: str
    kind: EventKind
    count: int
    first_seen: datetime
    last_seen: datetime


@dataclass(slots=True)
class _Entry:
    template_text: str
    kind: EventKind
    count: int
    first_seen: datetime
    last_seen: datetime


@dataclass(slots=True)
class _SystemState:
    miner: TemplateMiner
    cache: dict[str, str]
    registry: dict[str, _Entry]
    dirty: bool = False


def _miner_config(max_clusters: int) -> TemplateMinerConfig:
    """An explicit configuration so Drain3 reads no ``drain3.ini`` from the working directory."""
    config = TemplateMinerConfig()
    config.drain_sim_th = 0.4
    config.drain_depth = 4
    config.drain_max_children = 100
    config.drain_max_clusters = max_clusters
    config.drain_extra_delimiters = []
    config.masking_instructions = []
    config.parametrize_numeric_tokens = True
    config.profiling_enabled = False
    config.snapshot_compress_state = False
    return config


# ---------------------------------------------------------------------------------------------
# Plain-JSON state (no jsonpickle)
# ---------------------------------------------------------------------------------------------


def _node_to_json(node: Node, depth: int) -> dict[str, Any]:
    if depth > _MAX_TREE_DEPTH:
        msg = "drain tree deeper than expected"
        raise ValueError(msg)
    return {
        "k": {
            key: _node_to_json(child, depth + 1) for key, child in node.key_to_child_node.items()
        },
        "c": list(node.cluster_ids),
    }


def _node_from_json(data: Any, depth: int) -> Node:
    if depth > _MAX_TREE_DEPTH or not isinstance(data, dict):
        msg = "invalid drain tree"
        raise ValueError(msg)
    children = data.get("k", {})
    ids = data.get("c", [])
    if not isinstance(children, dict) or not isinstance(ids, list):
        msg = "invalid drain tree node"
        raise ValueError(msg)
    node = Node()
    for key, child in children.items():
        if not isinstance(key, str):
            msg = "invalid drain tree key"
            raise ValueError(msg)
        node.key_to_child_node[key] = _node_from_json(child, depth + 1)
    for cluster_id in ids:
        if not isinstance(cluster_id, int) or isinstance(cluster_id, bool) or cluster_id < 1:
            msg = "invalid cluster id in drain tree"
            raise ValueError(msg)
        node.cluster_ids.append(cluster_id)
    return node


def _drain_to_json(drain: Drain) -> dict[str, Any]:
    clusters = [
        {
            "id": cluster.cluster_id,
            "tokens": list(cluster.log_template_tokens),
            "size": cluster.size,
        }
        for cluster in drain.id_to_cluster.values()
    ]
    return {
        "tree": _node_to_json(drain.root_node, 0),
        "clusters": clusters,
        "counter": drain.clusters_counter,
    }


def _drain_from_json(drain: Drain, data: dict[str, Any]) -> None:
    clusters = data.get("clusters")
    counter = data.get("counter")
    if not isinstance(clusters, list) or not isinstance(counter, int) or counter < 0:
        msg = "invalid drain state"
        raise ValueError(msg)
    root = _node_from_json(data.get("tree"), 0)
    restored: list[LogCluster] = []
    for item in clusters:
        if not isinstance(item, dict):
            msg = "invalid cluster"
            raise ValueError(msg)
        cluster_id = item.get("id")
        tokens = item.get("tokens")
        size = item.get("size", 1)
        if (
            not isinstance(cluster_id, int)
            or isinstance(cluster_id, bool)
            or cluster_id < 1
            or cluster_id > counter
            or not isinstance(tokens, list)
            or not all(isinstance(token, str) and token for token in tokens)
            or not isinstance(size, int)
            or size < 1
        ):
            msg = "invalid cluster"
            raise ValueError(msg)
        cluster = LogCluster(tokens, cluster_id)
        cluster.size = size
        restored.append(cluster)
    for cluster in restored:
        drain.id_to_cluster[cluster.cluster_id] = cluster
    drain.clusters_counter = counter
    drain.root_node = root


def _registry_to_json(registry: dict[str, _Entry]) -> list[dict[str, Any]]:
    return [
        {
            "id": template_id,
            "text": entry.template_text,
            "kind": entry.kind.value,
            "count": entry.count,
            "first": entry.first_seen.isoformat(),
            "last": entry.last_seen.isoformat(),
        }
        for template_id, entry in registry.items()
    ]


def _aware(text: Any) -> datetime:
    if not isinstance(text, str):
        msg = "invalid registry timestamp"
        raise ValueError(msg)
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        msg = "naive registry timestamp"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _registry_from_json(data: Any) -> dict[str, _Entry]:
    if not isinstance(data, list):
        msg = "invalid registry"
        raise ValueError(msg)
    registry: dict[str, _Entry] = {}
    for item in data:
        if not isinstance(item, dict):
            msg = "invalid registry entry"
            raise ValueError(msg)
        template_id = item.get("id")
        text = item.get("text")
        count = item.get("count")
        kind = item.get("kind")
        if (
            not isinstance(template_id, str)
            or not isinstance(text, str)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(kind, str)
        ):
            msg = "invalid registry entry"
            raise ValueError(msg)
        registry[template_id] = _Entry(
            template_text=text[:MAX_TEMPLATE_TEXT_LEN],
            kind=EventKind(kind),
            count=count,
            first_seen=_aware(item.get("first")),
            last_seen=_aware(item.get("last")),
        )
    return registry


class TemplateStore:
    """Drain3 miners, template ids and the template registry, per system (spec 8.2 item 6).

    ``directory`` is ``EdgeSettings.templates_dir``; ``None`` keeps everything in memory (tests,
    one-shot runs). Not thread-safe: the gateway owns one store per pipeline worker.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL,
        max_clusters: int = DEFAULT_MAX_CLUSTERS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self._directory = directory
        self._flush_interval = max(0.0, flush_interval_seconds)
        self._max_clusters = max(1, max_clusters)
        self._cache_size = max(16, cache_size)
        self._states: dict[str, _SystemState] = {}
        self._ids: dict[tuple[str, str], str] = {}
        self._last_flush = time.monotonic()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- mining -------------------------------------------------------------------------------

    def mine(self, system_id: str, message: str) -> tuple[str, list[str]]:
        """The template of ``message`` in ``system_id`` and its parameters, in order.

        Whitespace runs are normalised; tokens with a digit or ``@`` are parameters from the
        first sighting; other variable tokens become parameters once Drain3 has seen a second
        variant. The template is capped at ``MAX_TEMPLATE_TEXT_LEN``; an empty message has the
        empty template.
        """
        state = self._state(system_id)
        tokens = message.split()
        if not tokens:
            return "", []
        if len(tokens) > MAX_MESSAGE_TOKENS:
            tokens = tokens[:MAX_MESSAGE_TOKENS]
        masked = " ".join(MASK if _mask_token(token) else token for token in tokens)
        template = state.cache.get(masked)
        if template is None:
            result = state.miner.add_log_message(masked)
            template = str(result["template_mined"])
            if result["change_type"] == _CHANGED or len(state.cache) >= self._cache_size:
                state.cache.clear()
            state.cache[masked] = template
            state.dirty = True
            self._maybe_flush()
        template_tokens = template.split(" ")
        params: list[str] = []
        if len(template_tokens) == len(tokens):
            params = [
                token
                for token, marker in zip(tokens, template_tokens, strict=True)
                if marker == MASK
            ]
        if len(template) > MAX_TEMPLATE_TEXT_LEN:
            template = template[:MAX_TEMPLATE_TEXT_LEN]
        return template, params

    def template_id(self, system_id: str, template_text: str) -> str:
        """:func:`compute_template_id`, memoised per (system, text)."""
        key = (system_id, template_text)
        template_id = self._ids.get(key)
        if template_id is None:
            if len(self._ids) >= 65_536:
                self._ids.clear()
            template_id = compute_template_id(system_id, template_text)
            self._ids[key] = template_id
        return template_id

    def cluster_count(self, system_id: str) -> int:
        """Live Drain3 clusters for ``system_id`` (bounded by ``max_clusters``)."""
        return len(self._state(system_id).miner.drain.clusters)

    # -- registry -----------------------------------------------------------------------------

    def register(
        self,
        system_id: str,
        template_id: str,
        template_text: str,
        kind: EventKind,
        seen_at: datetime,
    ) -> None:
        """Count one record of ``template_id`` observed at ``seen_at`` (aware)."""
        if seen_at.tzinfo is None:
            msg = "seen_at must be timezone-aware"
            raise ValueError(msg)
        seen = seen_at.astimezone(UTC)
        state = self._state(system_id)
        entry = state.registry.get(template_id)
        if entry is None:
            state.registry[template_id] = _Entry(
                template_text=template_text[:MAX_TEMPLATE_TEXT_LEN],
                kind=kind,
                count=1,
                first_seen=seen,
                last_seen=seen,
            )
        else:
            entry.count += 1
            if seen < entry.first_seen:
                entry.first_seen = seen
            if seen > entry.last_seen:
                entry.last_seen = seen
        state.dirty = True

    def registry(self) -> list[TemplateRecord]:
        """Every template seen, sorted by system then template id (for ``templates.json``)."""
        records = [
            TemplateRecord(
                template_id=template_id,
                system_id=system_id,
                template_text=entry.template_text,
                kind=entry.kind,
                count=entry.count,
                first_seen=entry.first_seen,
                last_seen=entry.last_seen,
            )
            for system_id, state in self._states.items()
            for template_id, entry in state.registry.items()
        ]
        records.sort(key=lambda record: (record.system_id, record.template_id))
        return records

    # -- persistence --------------------------------------------------------------------------

    def flush(self) -> None:
        """Write every changed system's state to its file; no-op without a directory."""
        self._last_flush = time.monotonic()
        if self._directory is None:
            return
        for system_id, state in self._states.items():
            if state.dirty:
                self._write(system_id, state)
                state.dirty = False

    def close(self) -> None:
        """Flush; the store stays usable afterwards."""
        self.flush()

    def _maybe_flush(self) -> None:
        if self._directory is None:
            return
        if time.monotonic() - self._last_flush >= self._flush_interval:
            self.flush()

    def _path(self, system_id: str) -> Path:
        if self._directory is None:  # callers check
            msg = "the template store has no directory"
            raise RuntimeError(msg)
        return self._directory / f"{system_id}.json"

    def _write(self, system_id: str, state: _SystemState) -> None:
        path = self._path(system_id)
        payload = {
            "version": STATE_VERSION,
            "system_id": system_id,
            **_drain_to_json(state.miner.drain),
            "registry": _registry_to_json(state.registry),
        }
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError as exc:
            _log().warning(
                "template_state_write_failed", system_id=system_id, error=type(exc).__name__
            )
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _load(self, system_id: str, state: _SystemState) -> None:
        path = self._path(system_id)
        try:
            if not path.is_file():
                return
            if path.stat().st_size > MAX_STATE_BYTES:
                _log().warning("template_state_too_large", system_id=system_id, path=str(path))
                return
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                msg = "unknown template state version"
                raise ValueError(msg)
            registry = _registry_from_json(data.get("registry", []))
            _drain_from_json(state.miner.drain, data)
            state.registry = registry
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            _log().warning(
                "template_state_unreadable",
                system_id=system_id,
                path=str(path),
                error=type(exc).__name__,
            )
            state.miner = TemplateMiner(config=_miner_config(self._max_clusters))
            state.registry = {}

    def _state(self, system_id: str) -> _SystemState:
        state = self._states.get(system_id)
        if state is None:
            if not SOURCE_ID_PATTERN.match(system_id):
                msg = "system_id must match the source id pattern"
                raise ValueError(msg)
            state = _SystemState(
                miner=TemplateMiner(config=_miner_config(self._max_clusters)),
                cache={},
                registry={},
            )
            if self._directory is not None:
                self._load(system_id, state)
            self._states[system_id] = state
        return state


def _log() -> Any:
    return get_logger(component="templates")
