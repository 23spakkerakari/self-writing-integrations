"""Identifier forms, the token format and value shapes (spec 8.3, 8.4).

A *form* names the transformation of an identifier value that a token stands for (spec 8.4). The
*token* is the HMAC of the form value, prefixed with the key version. The *shape* is the coarse
value signature the edge keeps per field for classification (spec 8.3). Everything here is a pure
string function so the edge, core, the simulator and the eval harness share one definition.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, Field

TokenDomain = Literal["id", "date", "amt", "ph"]
"""HMAC domain separator mixed into a token before the form value (spec 8.4)."""

BASE_FORMS: Final[frozenset[str]] = frozenset({"raw", "norm", "alnum", "date", "amount"})
"""Forms without an index."""

DIGITS_MAX_K: Final = 2
"""Highest ``digits.<k>`` index: spec 8.4 allows at most three digit runs."""

PHONETIC_MAX_K: Final = 7
"""Highest ``phonetic.<k>`` index: one Double Metaphone code per name token, up to eight."""

# The index character classes assume single-digit maxima; both constants are below 10.
FORM_PATTERN: Final = re.compile(
    rf"^(?:raw|norm|alnum|date|amount|digits\.[0-{DIGITS_MAX_K}]|phonetic\.[0-{PHONETIC_MAX_K}])$"
)
"""Every form name per spec 8.4. Lowercase only; no whitespace, no leading zeros in the index."""

TOKEN_PATTERN: Final = re.compile(r"^t[1-9][0-9]{0,3}\.[A-Za-z0-9_-]{22}$")
"""``t<key_version>.<22 base64url chars>`` (spec 8.4): the key version is 1 to 4 digits without a
leading zero, the body is the first 22 characters of base64url(HMAC-SHA256) with no padding."""

SHAPE_MAX_LEN: Final = 64
"""Longest shape the canonical event carries; :func:`shape` cuts its result at this length."""

SHAPE_MAX_RUN: Final = 12
"""Runs of one shape character longer than this collapse to this many plus ``+`` (spec 8.3)."""

_DOMAIN_BY_BASE: Final[dict[str, TokenDomain]] = {
    "raw": "id",
    "norm": "id",
    "alnum": "id",
    "digits": "id",
    "date": "date",
    "amount": "amt",
    "phonetic": "ph",
}

Form = Annotated[str, Field(pattern=FORM_PATTERN.pattern)]
"""A form name (spec 8.4) as a model field; the JSON Schema carries the same pattern."""

Token = Annotated[str, Field(pattern=TOKEN_PATTERN.pattern)]
"""A token (spec 8.4) as a model field; the JSON Schema carries the same pattern."""


def is_form(name: str) -> bool:
    """Return whether ``name`` is one of the form names of spec 8.4."""
    return FORM_PATTERN.fullmatch(name) is not None


def parse_form(name: str) -> tuple[str, int | None]:
    """Split a form name into its base and optional index (spec 8.4).

    ``"digits.1"`` gives ``("digits", 1)`` and ``"raw"`` gives ``("raw", None)``. Anything that is
    not a form name raises :class:`ValueError`.
    """
    if not is_form(name):
        msg = f"not a form name: {name!r}"
        raise ValueError(msg)
    base, separator, index = name.partition(".")
    return (base, int(index)) if separator else (base, None)


def token_domain(name: str) -> TokenDomain:
    """Return the HMAC domain of a form (spec 8.4).

    ``raw``, ``norm``, ``alnum`` and ``digits.<k>`` share the ``id`` domain so that ``4471`` seen
    as a raw value and ``4471`` extracted as a digit run tokenize identically. ``date`` maps to
    ``date``, ``amount`` to ``amt`` and ``phonetic.<k>`` to ``ph``.
    """
    base, _index = parse_form(name)
    return _DOMAIN_BY_BASE[base]


def shape(value: str) -> str:
    """Return the shape of a value (spec 8.3).

    Digits become ``9``, letters become ``A``, punctuation and whitespace stay as they are. A run
    of more than :data:`SHAPE_MAX_RUN` identical shape characters collapses to that many followed
    by ``+``. The result is cut at :data:`SHAPE_MAX_LEN` characters so it always fits
    ``Identifier.shape``. ``"SO-0004471"`` gives ``"AA-9999999"``; twenty digits give twelve
    ``9`` and a ``+``. The function is idempotent.
    """
    out: list[str] = []
    run_char = ""
    run_len = 0
    for char in value:
        if char.isdigit():
            mapped = "9"
        elif char.isalpha():
            mapped = "A"
        else:
            mapped = char
        run_len = run_len + 1 if mapped == run_char else 1
        run_char = mapped
        if run_len <= SHAPE_MAX_RUN:
            out.append(mapped)
        elif run_len == SHAPE_MAX_RUN + 1:
            out.append("+")
        if len(out) >= SHAPE_MAX_LEN:
            break
    return "".join(out)


def _require_shape(value: str) -> str:
    """Accept only a fixed point of :func:`shape`. The message never echoes the value."""
    if shape(value) != value:
        msg = "not a shape: expected the output of shape() (digits as '9', letters as 'A')"
        raise ValueError(msg)
    return value


Shape = Annotated[
    str, Field(min_length=1, max_length=SHAPE_MAX_LEN), AfterValidator(_require_shape)
]
"""A value shape (spec 8.3) as a model field: 1 to 64 characters that :func:`shape` maps to
themselves, so a raw identifier value cannot travel in a shape slot (spec 2.3 invariant 2). The
JSON Schema carries the length bounds only; the fixed-point rule is model-only."""
