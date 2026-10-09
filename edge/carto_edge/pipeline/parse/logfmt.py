"""logfmt records (spec 8.2 item 3).

``key=value`` pairs separated by whitespace. A value is bare (up to the next whitespace) or
double-quoted with ``\\"`` and ``\\\\`` escapes (``\\n``, ``\\t``, ``\\r`` are honoured too, as
Go's logfmt writes them); an unterminated quote runs to the end of the line. A bare key (no
``=``) is a flag and gets the value ``true``. A key cannot be empty and cannot contain
whitespace, ``=`` or a quote. A line is logfmt only when its first token is a ``key=`` pair and
every token is a pair or a flag; otherwise :func:`parse_logfmt` returns ``None`` so the next
parser in spec 8.2 order gets a turn. The scanner is a single pass over the characters: no
regular expressions, linear time.
"""

from __future__ import annotations

from typing import Final

__all__ = ["looks_like_logfmt", "parse_logfmt"]

_KEY_FORBIDDEN: Final = frozenset('="')
_ESCAPES: Final = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
_FLAG_VALUE: Final = "true"


def looks_like_logfmt(text: str) -> bool:
    """Cheap sniff for the auto-detector: the first token is ``key=...`` with a non-empty key."""
    head = text.lstrip()
    end = 0
    while end < len(head) and not head[end].isspace() and head[end] not in _KEY_FORBIDDEN:
        end += 1
    return end > 0 and end < len(head) and head[end] == "="


def _scan_quoted(text: str, start: int) -> tuple[str, int]:
    """Scan a quoted value whose opening quote is at ``start``; return the value and the
    position after the closing quote (or the end of the text)."""
    pieces: list[str] = []
    index = start + 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            pieces.append(_ESCAPES.get(text[index + 1], "\\" + text[index + 1]))
            index += 2
            continue
        if char == '"':
            return "".join(pieces), index + 1
        pieces.append(char)
        index += 1
    return "".join(pieces), length


def parse_logfmt(text: str) -> dict[str, str] | None:
    """Parse a logfmt line into ``key -> value``; ``None`` when the line is not logfmt."""
    fields: dict[str, str] = {}
    index = 0
    length = len(text)
    pairs = 0
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in _KEY_FORBIDDEN:
            index += 1
        key = text[start:index]
        if not key:
            return None
        if index >= length or text[index].isspace():
            fields[key] = _FLAG_VALUE
            continue
        if text[index] != "=":
            return None
        index += 1
        if index < length and text[index] == '"':
            value, index = _scan_quoted(text, index)
        else:
            start = index
            while index < length and not text[index].isspace():
                index += 1
            value = text[start:index]
        if pairs == 0 and fields:
            return None
        fields[key] = value
        pairs += 1
    if pairs == 0:
        return None
    return fields
