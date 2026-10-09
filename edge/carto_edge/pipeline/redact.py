"""Redaction of what leaves the edge in clear: template constants and kept attribute values.

Spec 8.3: "Presidio runs on template constants as well, in case a constant contains PII."
:func:`redact_template_text` runs the configured detector over a template's text and replaces
every hit by ``<ENTITY_TYPE>`` (:func:`carto_edge.pipeline.pii.mask`); the count feeds
``redaction.entities_masked`` in the canonical event (spec 7.1). Results are memoized per
``(detector, text)`` in a bounded LRU because a template repeats for thousands of events.

Spec 7.1: "attributes: only fields classified as low-cardinality and non-sensitive. Values
truncated to 256 chars." :func:`truncate_attribute` is that cut and :func:`attribute_is_clean`
is the last check before a low-cardinality value is kept: no detector hit and no secret-shaped
content (:func:`carto_edge.pipeline.pii.looks_like_secret`).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Final, NamedTuple

from carto_edge.pipeline.pii import PiiDetector, looks_like_secret, mask, truncate_text

__all__ = [
    "MAX_ATTRIBUTE_LEN",
    "TEMPLATE_CACHE_SIZE",
    "CacheInfo",
    "attribute_is_clean",
    "redact_template_text",
    "template_cache_clear",
    "template_cache_info",
    "truncate_attribute",
]

MAX_ATTRIBUTE_LEN: Final = 256
"""Spec 7.1: attribute values are truncated to 256 characters."""

TEMPLATE_CACHE_SIZE: Final = 4096
"""Memoized template redactions (one entry per distinct template text and detector)."""


class CacheInfo(NamedTuple):
    hits: int
    misses: int
    maxsize: int
    currsize: int


def truncate_attribute(value: str) -> str:
    """Trim surrounding whitespace and cut at :data:`MAX_ATTRIBUTE_LEN` characters."""
    return value.strip()[:MAX_ATTRIBUTE_LEN]


def attribute_is_clean(value: str, detector: PiiDetector) -> bool:
    """Whether the (truncated) value may travel in clear: empty, or no detector hit and no
    secret-shaped content."""
    text = truncate_attribute(value)
    if not text:
        return True
    if looks_like_secret(text):
        return False
    return not detector.detect(text)


class _TemplateCache:
    """Bounded LRU of ``(detector, text) -> (masked text, entities masked)``.

    Keyed by the detector's identity; the entry keeps a reference to the detector so its id
    cannot be reused by another object while the entry lives.
    """

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._entries: OrderedDict[tuple[int, str], tuple[PiiDetector, str, int]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def redact(self, text: str, detector: PiiDetector) -> tuple[str, int]:
        key = (id(detector), text)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return entry[1], entry[2]
            self.misses += 1
        cut = truncate_text(text)
        masked, count = mask(cut, detector.detect(cut))
        with self._lock:
            self._entries[key] = (detector, masked, count)
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)
        return masked, count

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0

    def info(self) -> CacheInfo:
        with self._lock:
            return CacheInfo(self.hits, self.misses, self.maxsize, len(self._entries))


_CACHE: Final = _TemplateCache(TEMPLATE_CACHE_SIZE)


def redact_template_text(text: str, detector: PiiDetector) -> tuple[str, int]:
    """Mask PII in a template's constants. Returns the masked text (cut at 4 KB like every
    detector input) and the number of entities masked. Memoized per text and detector."""
    return _CACHE.redact(text, detector)


def template_cache_clear() -> None:
    _CACHE.clear()


def template_cache_info() -> CacheInfo:
    """Cache statistics: hits, misses, maxsize, currsize."""
    return _CACHE.info()
