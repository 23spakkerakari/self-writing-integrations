"""HTTP access logs: Apache/nginx combined and common formats, custom RE2 patterns (spec 8.2
item 5, ADR 0016).

The built-in pattern covers the common format and its combined extension (referer and user
agent), with trailing extra fields tolerated. A per-source ``access_log_pattern`` is compiled
with ``google-re2`` (linear time whatever the customer writes, spec 2.3 invariant 8) and must
use named groups; its ``groupdict`` becomes the fields. Either way the template is
``<METHOD> <generalized path> <status class>`` (ADR 0016): the query string is dropped and
digit runs, UUIDs and long hex segments in the path become ``*``, so ``GET /orders/4471/items
2xx`` and ``GET /orders/9910/items 2xx`` are one node. Dash placeholders (``-``) of the
standard formats are absent fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import re2

__all__ = [
    "AccessLogParser",
    "AccessLogRecord",
    "generalize_path",
    "looks_like_access_log",
    "status_class",
]

MAX_PATH_LEN: Final = 512

_STANDARD: Final = re.compile(
    r"^(?P<remote_addr>\S+) (?P<remote_ident>\S+) (?P<remote_user>\S+) "
    r"\[(?P<time>[^\]]{1,64})\] \"(?P<request>[^\"]{0,4096})\" (?P<status>\d{3}|-) "
    r"(?P<bytes>\d{1,20}|-)"
    r"(?: \"(?P<referer>[^\"]{0,4096})\" \"(?P<user_agent>[^\"]{0,4096})\")?(?: .{0,4096})?$"
)
_PREFIX: Final = re.compile(r"^\S+ \S+ \S+ \[")
_UUID: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX: Final = re.compile(r"^[0-9a-fA-F]{16,}$")
_DIGITS: Final = re.compile(r"\d+")
_DASH_IS_ABSENT: Final = frozenset(
    {"remote_ident", "remote_user", "bytes", "referer", "user_agent"}
)


@dataclass(frozen=True, slots=True)
class AccessLogRecord:
    """The fields of one access-log line and its template text."""

    fields: dict[str, str]
    template: str


def looks_like_access_log(text: str) -> bool:
    """Cheap sniff for the auto-detector: ``host ident user [`` at the start."""
    return _PREFIX.match(text) is not None


def status_class(status: str) -> str:
    """``200`` -> ``2xx``; anything that is not a three-digit code -> ``-``."""
    text = status.strip()
    if len(text) == 3 and text.isdigit():
        return f"{text[0]}xx"
    return "-"


def generalize_path(path: str) -> str:
    """The route of a request path (ADR 0016): no query or fragment, digit runs, UUIDs and
    hex segments of 16 or more characters replaced by ``*``; ``-`` for an empty path."""
    if not path:
        return "-"
    cut = len(path)
    for stop in ("?", "#"):
        index = path.find(stop)
        if 0 <= index < cut:
            cut = index
    route = path[:cut][:MAX_PATH_LEN]
    if not route:
        return "-"
    pieces: list[str] = []
    for segment in route.split("/"):
        if _UUID.match(segment) or _HEX.match(segment):
            pieces.append("*")
        else:
            pieces.append(_DIGITS.sub("*", segment))
    return "/".join(pieces)


def _template(fields: dict[str, str]) -> str:
    method = fields.get("method") or "-"
    path = fields.get("path") or ""
    return f"{method} {generalize_path(path)} {status_class(fields.get('status', ''))}"


def _split_request(request: str, fields: dict[str, str]) -> None:
    parts = request.split(" ")
    if len(parts) in (2, 3) and parts[0] and parts[1]:
        fields["method"] = parts[0]
        fields["path"] = parts[1]
        if len(parts) == 3 and parts[2]:
            fields["protocol"] = parts[2]
        return
    fields["request"] = request


class AccessLogParser:
    """One per source: the built-in standard formats, or the configured RE2 pattern."""

    def __init__(self, pattern: str | None = None) -> None:
        self._custom: Any | None = None
        if pattern is not None:
            options = re2.Options()
            options.log_errors = False
            options.max_mem = 16 * 1024 * 1024
            try:
                compiled = re2.compile(pattern, options=options)
            except re2.error as exc:
                msg = "invalid access_log_pattern (RE2 syntax; no backreferences or lookarounds)"
                raise ValueError(msg) from exc
            if not compiled.groupindex:
                msg = "access_log_pattern needs at least one named group, e.g. (?P<path>\\S+)"
                raise ValueError(msg)
            self._custom = compiled

    def parse(self, text: str) -> AccessLogRecord | None:
        """Fields and template of one line, or ``None`` when it does not match."""
        line = text.rstrip("\r\n")
        if not line:
            return None
        if self._custom is not None:
            return self._parse_custom(line)
        match = _STANDARD.match(line)
        if match is None:
            return None
        fields: dict[str, str] = {}
        for name, value in match.groupdict().items():
            if value is None or name == "request":
                continue
            if value == "-" and name in _DASH_IS_ABSENT:
                continue
            fields[name] = value
        _split_request(match.group("request"), fields)
        return AccessLogRecord(fields=fields, template=_template(fields))

    def _parse_custom(self, line: str) -> AccessLogRecord | None:
        assert self._custom is not None  # noqa: S101 - established by the constructor
        try:
            match = self._custom.search(line)
        except (re2.error, UnicodeError, ValueError, TypeError):
            return None
        if match is None:
            return None
        fields = {name: value for name, value in match.groupdict().items() if value is not None}
        if not fields:
            return None
        return AccessLogRecord(fields=fields, template=_template(fields))
