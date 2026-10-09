"""Format auto-detection for ``format: auto`` sources (spec 8.2 "order of attempts per record,
first match wins").

:func:`sniff` lists the parsers that can possibly accept a line, in spec order, from cheap
prefix checks (``{`` for JSON, ``<`` for XML, a leading ``key=`` for logfmt, ``host ident user
[`` for access logs); unstructured text is always last because it always accepts. CSV has no
per-line shape and joins the candidates only when the source already knows its header
(``csv_ready``). :class:`FormatDetector` remembers what worked: after ``lock_after``
consecutive records of one format the source is locked to it and that parser is tried first,
which is the fast path for homogeneous sources; a line it rejects still falls through the
sniffed candidates, and a run of records in another format moves the lock. A lock on text
never reorders anything, since text would then swallow every structured line.
"""

from __future__ import annotations

from typing import Final

from carto_edge.config import RecordFormat
from carto_edge.pipeline.parse.access_log import looks_like_access_log
from carto_edge.pipeline.parse.json import looks_like_json
from carto_edge.pipeline.parse.logfmt import looks_like_logfmt
from carto_edge.pipeline.parse.xml import looks_like_xml

__all__ = ["DEFAULT_LOCK_AFTER", "FormatDetector", "sniff"]

DEFAULT_LOCK_AFTER: Final = 5


def sniff(text: str) -> tuple[RecordFormat, ...]:
    """The formats whose parser could accept ``text``, in spec 8.2 order, text last."""
    candidates: list[RecordFormat] = []
    if looks_like_json(text):
        candidates.append(RecordFormat.JSON)
    if looks_like_xml(text):
        candidates.append(RecordFormat.XML)
    if looks_like_logfmt(text):
        candidates.append(RecordFormat.LOGFMT)
    if looks_like_access_log(text):
        candidates.append(RecordFormat.ACCESS_LOG)
    candidates.append(RecordFormat.TEXT)
    return tuple(candidates)


class FormatDetector:
    """Per-source memory of the format that keeps working (spec 8.2 per-source hints, learned)."""

    def __init__(self, lock_after: int = DEFAULT_LOCK_AFTER) -> None:
        self._lock_after = max(1, lock_after)
        self._locked: RecordFormat | None = None
        self._streak_format: RecordFormat | None = None
        self._streak = 0

    @property
    def locked(self) -> RecordFormat | None:
        """The format this source is locked to, once ``lock_after`` records agreed."""
        return self._locked

    def observe(self, fmt: RecordFormat) -> None:
        """Record that ``fmt`` parsed the latest record."""
        if fmt == self._streak_format:
            self._streak += 1
        else:
            self._streak_format = fmt
            self._streak = 1
        if self._streak >= self._lock_after and self._locked != fmt:
            self._locked = fmt

    def order(self, text: str, *, csv_ready: bool) -> tuple[RecordFormat, ...]:
        """The parsers to try for ``text``, locked format first, then the sniffed candidates
        (CSV among them when the header is known), text last."""
        candidates = list(sniff(text))
        if csv_ready:
            position = (
                candidates.index(RecordFormat.ACCESS_LOG)
                if RecordFormat.ACCESS_LOG in candidates
                else len(candidates) - 1
            )
            candidates.insert(position, RecordFormat.CSV)
        locked = self._locked
        if locked is not None and locked != RecordFormat.TEXT:
            if locked in candidates:
                candidates.remove(locked)
            candidates.insert(0, locked)
        return tuple(candidates)
