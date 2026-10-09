"""Identifier forms (spec 8.4 "Forms").

For every identifier-class value the edge computes the forms of the spec 8.4 table, and every
form becomes one token (:mod:`carto_edge.pipeline.tokenize`). The functions here are pure string
transformations so the edge, the search box tokenizer and the eval harness agree on every byte:

- ``raw``: the exact string after Unicode NFKC and trim (:func:`normalize`);
- ``norm``: ``raw`` lowercased;
- ``alnum``: ``norm`` with every non-alphanumeric character removed;
- ``digits.k``: the k-th run of four or more digits with leading zeros stripped, at most
  :data:`carto_schema.forms.DIGITS_MAX_K` + 1 runs;
- ``date``: the ISO 8601 date of a date or date-time (:func:`iso_date`);
- ``amount``: integer minor units of a decimal amount (:func:`amount_minor_units`);
- ``phonetic.k``: the Double Metaphone primary code of the k-th whitespace-separated name
  token (:func:`phonetic_codes`; ADR 0021).

Two rules close the table: a form that collapses to fewer than :data:`MIN_FORM_LEN` characters
is skipped, and a form whose value equals an earlier form's value in the same token domain is
not repeated. Phonetic codes are exempt from the length rule: Double Metaphone codes are at
most four characters and the spec's own example (``JN``) is two.

Properties (spec 18.1, tested with Hypothesis): :func:`normalize` is idempotent and
:func:`compute_forms` is deterministic. Nothing here logs, and no error message carries a value.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from itertools import islice
from typing import Final

from metaphone import doublemetaphone

from carto_edge.pipeline.model import FieldClass
from carto_schema.forms import DIGITS_MAX_K, PHONETIC_MAX_K, TokenDomain, parse_form, token_domain

__all__ = [
    "EXPANDING_FORMS",
    "MAX_FORM_VALUE_LEN",
    "MIN_FORM_LEN",
    "FormValue",
    "amount_minor_units",
    "compute_forms",
    "default_forms",
    "digit_runs",
    "iso_date",
    "normalize",
    "parse_form_request",
    "phonetic_codes",
]

MIN_FORM_LEN: Final = 3
"""Spec 8.4: "Forms that collapse to fewer than 3 characters are skipped"."""

MAX_FORM_VALUE_LEN: Final = 1024
"""Longest value forms are computed from; longer values are cut first (an identifier is never
this long, and the cut keeps :func:`compute_forms` linear on hostile input, spec 2.3 item 8)."""

EXPANDING_FORMS: Final[frozenset[str]] = frozenset({"digits", "phonetic"})
"""Form requests without an index: ``digits`` means every ``digits.k``, likewise ``phonetic``."""

MAX_DATE_TEXT_LEN: Final = 64
MAX_AMOUNT_TEXT_LEN: Final = 64
MAX_AMOUNT_INTEGER_DIGITS: Final = 18
YEAR_MIN: Final = 1900
YEAR_MAX: Final = 2199
EPOCH_SECONDS_MIN: Final = 1_000_000_000  # 2001-09-09
EPOCH_SECONDS_MAX: Final = 4_100_000_000  # 2099-12-05
MINOR_UNITS: Final = Decimal("0.01")

_ID_FORMS: Final = ("raw", "norm", "alnum", "digits")
_TEXT_FORMS: Final = ("raw", "norm", "alnum")

_DEFAULT_FORMS: Final[dict[FieldClass, tuple[str, ...]]] = {
    FieldClass.IDENTIFIER: _ID_FORMS,
    FieldClass.LOW_CARD_ATTRIBUTE: _TEXT_FORMS,
    FieldClass.TIMESTAMP: ("date",),
    FieldClass.AMOUNT: ("amount",),
    FieldClass.DATE: ("date",),
    FieldClass.PERSON_NAME: _TEXT_FORMS,
    FieldClass.CONTACT: _ID_FORMS,
    FieldClass.GOVERNMENT_ID: _ID_FORMS,
    FieldClass.FINANCIAL: _ID_FORMS,
    FieldClass.HEALTH: _TEXT_FORMS,
    FieldClass.FREE_TEXT: _TEXT_FORMS,
    FieldClass.SECRET_LIKE: (),
    FieldClass.UNKNOWN: _ID_FORMS,
}

_DIGIT_RUN: Final = re.compile(r"[0-9]{4,}")

_MONTHS: Final[dict[str, int]] = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_EPOCH: Final = re.compile(r"[0-9]{10}(?:[0-9]{3})?")
_YMD: Final = re.compile(r"([0-9]{4})[/.]([0-9]{1,2})[/.]([0-9]{1,2})")
_DMY_DOTTED: Final = re.compile(r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})")
_XXY_SLASHED: Final = re.compile(r"([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{4})")
_D_MON_Y: Final = re.compile(r"([0-9]{1,2})[ -]([A-Za-z]{3,9})\.?,?[ -]([0-9]{4})")
_MON_D_Y: Final = re.compile(r"([A-Za-z]{3,9})\.? ([0-9]{1,2}),? ([0-9]{4})")

_CURRENCY_SYMBOLS: Final = "$\u20ac\u00a3\u00a5\u20b9\u20bd\u20a9\u20aa\u20ab\u20a6\u20b1\u0e3f"
_CURRENCY_CODE_PREFIX: Final = re.compile(r"^[A-Za-z]{3}(?![A-Za-z0-9])\s*")
_CURRENCY_CODE_SUFFIX: Final = re.compile(r"\s*(?<![A-Za-z0-9])[A-Za-z]{3}$")
_AMOUNT_BODY: Final = re.compile(r"[0-9][0-9.,' \u00a0]*|[.,][0-9]+")
_GROUP_SEPARATORS: Final = re.compile(r"[ \u00a0']")


@dataclass(frozen=True, slots=True)
class FormValue:
    """One computed form of a value: the form name (spec 8.4) and the text the token is of."""

    form: str
    value: str


def default_forms(field_class: FieldClass) -> tuple[str, ...]:
    """The forms the edge computes for a class when the policy names none."""
    return _DEFAULT_FORMS[field_class]


def parse_form_request(name: str) -> tuple[str, int | None]:
    """Split a requested form into base and index.

    Accepts every spec 8.4 form name plus the bare ``digits`` and ``phonetic``, which stand for
    every index. The message of the :class:`ValueError` never repeats the input.
    """
    if name in EXPANDING_FORMS:
        return name, None
    try:
        return parse_form(name)
    except ValueError as exc:
        msg = "not a form name (spec 8.4)"
        raise ValueError(msg) from exc


def normalize(value: str) -> str:
    """The ``raw`` form: Unicode NFKC, then surrounding whitespace removed. Idempotent."""
    return unicodedata.normalize("NFKC", value).strip()


def digit_runs(raw: str) -> list[str]:
    """Runs of four or more ASCII digits, leading zeros stripped, at most ``DIGITS_MAX_K + 1``.

    The list is positional: a run that collapses to nothing keeps its index so ``digits.k``
    names the same run whatever the other runs look like.
    """
    matches = islice(_DIGIT_RUN.finditer(raw), DIGITS_MAX_K + 1)
    return [match.group().lstrip("0") for match in matches]


def phonetic_codes(raw: str) -> list[str]:
    """Double Metaphone primary codes of the whitespace-separated name tokens (ADR 0021).

    Positional like :func:`digit_runs`; a token without a code (digits, punctuation) gives
    ``""``. At most ``PHONETIC_MAX_K + 1`` tokens are considered.
    """
    codes: list[str] = []
    for word in raw.split()[: PHONETIC_MAX_K + 1]:
        try:
            primary, _secondary = doublemetaphone(word)
        except (ValueError, IndexError, KeyError, TypeError):
            primary = ""
        codes.append(primary if isinstance(primary, str) else "")
    return codes


# ---------------------------------------------------------------------------------------------
# date
# ---------------------------------------------------------------------------------------------


def _date_of(moment: datetime) -> date:
    """The calendar date in UTC for aware values, as written for naive ones."""
    if moment.tzinfo is not None and moment.utcoffset() is not None:
        return moment.astimezone(UTC).date()
    return moment.date()


def _slashed(first: str, second: str, year: str) -> date | None:
    """``a/b/yyyy``: month first unless the first part cannot be a month (then day first)."""
    a, b, y = int(first), int(second), int(year)
    month, day = (b, a) if a > 12 else (a, b)
    return _safe_date(y, month, day)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_date(text: str) -> date | None:
    if _EPOCH.fullmatch(text):
        number = int(text)
        seconds = number // 1000 if len(text) == 13 else number
        if EPOCH_SECONDS_MIN <= seconds < EPOCH_SECONDS_MAX:
            return datetime.fromtimestamp(seconds, UTC).date()
        return None
    try:
        return _date_of(datetime.fromisoformat(text))
    except ValueError:
        pass
    if match := _YMD.fullmatch(text):
        return _safe_date(int(match[1]), int(match[2]), int(match[3]))
    if match := _DMY_DOTTED.fullmatch(text):
        return _safe_date(int(match[3]), int(match[2]), int(match[1]))
    if match := _XXY_SLASHED.fullmatch(text):
        return _slashed(match[1], match[2], match[3])
    if match := _D_MON_Y.fullmatch(text):
        month = _MONTHS.get(match[2].lower())
        return _safe_date(int(match[3]), month, int(match[1])) if month else None
    if match := _MON_D_Y.fullmatch(text):
        month = _MONTHS.get(match[1].lower())
        return _safe_date(int(match[3]), month, int(match[2])) if month else None
    try:
        return _date_of(parsedate_to_datetime(text))
    except (ValueError, TypeError, IndexError, OverflowError):
        return None


def iso_date(text: str) -> str | None:
    """The ``date`` form: ``YYYY-MM-DD`` of a date or date-time, or ``None`` when ``text`` is
    not one.

    Accepts ISO 8601 (extended and basic, with or without time and offset), ``YYYY/MM/DD``,
    ``DD.MM.YYYY``, ``MM/DD/YYYY`` (day first only when the first part exceeds 12),
    ``DD-Mon-YYYY``, ``Mon DD, YYYY``, ``DD Month YYYY``, RFC 2822 and epoch seconds or
    milliseconds. A date-time with an offset gives its UTC date. Years outside 1900 to 2199
    are rejected.
    """
    text = text.strip()
    if not text or len(text) > MAX_DATE_TEXT_LEN:
        return None
    parsed = _parse_date(text)
    if parsed is None or not YEAR_MIN <= parsed.year <= YEAR_MAX:
        return None
    return parsed.isoformat()


# ---------------------------------------------------------------------------------------------
# amount
# ---------------------------------------------------------------------------------------------


def _split_amount(text: str) -> tuple[str, str] | None:
    """Return (integer digits, fraction digits) of a formatted number, or ``None``.

    Decimal and thousands separators are resolved from the text alone: with both ``.`` and
    ``,`` present the last one is the decimal separator; a lone ``,`` followed by exactly
    three digits groups thousands and otherwise (one or two digits) is a decimal comma; a lone
    ``.`` is always a decimal point unless it repeats as a thousands separator. Spaces and
    apostrophes only ever group thousands. Groups must be three digits long.
    """
    dot, comma = text.rfind("."), text.rfind(",")
    if dot >= 0 and comma >= 0:
        decimal_sep, group_sep = (".", ",") if dot > comma else (",", ".")
        integer, _, fraction = text.rpartition(decimal_sep)
        if decimal_sep in integer:
            return None
        groups = integer.split(group_sep)
    elif comma >= 0:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            groups, fraction = [parts[0]], parts[1]
        else:
            groups, fraction = parts, ""
    elif dot >= 0:
        parts = text.split(".")
        if len(parts) == 2:
            groups, fraction = [parts[0]], parts[1]
        else:
            groups, fraction = parts, ""
    else:
        groups, fraction = [text], ""
    pieces = [piece for group in groups for piece in _GROUP_SEPARATORS.split(group)]
    if len(pieces) > 1 and (
        not pieces[0] or len(pieces[0]) > 3 or any(len(piece) != 3 for piece in pieces[1:])
    ):
        return None
    integer_digits = "".join(pieces)
    if not integer_digits.isdigit() and integer_digits != "":
        return None
    if fraction and not fraction.isdigit():
        return None
    if not integer_digits and not fraction:
        return None
    if len(integer_digits) > MAX_AMOUNT_INTEGER_DIGITS:
        return None
    return integer_digits, fraction


def amount_minor_units(text: str) -> int | None:
    """The ``amount`` form as an integer: minor units (cents) of a decimal amount.

    Currency symbols and three-letter codes around the number are ignored, thousands
    separators are removed (see :func:`_split_amount`), parentheses or a sign make the amount
    negative, and amounts with more than two fraction digits are rounded half up. Returns
    ``None`` for anything that is not a plain number.
    """
    text = text.strip()
    if not text or len(text) > MAX_AMOUNT_TEXT_LEN:
        return None
    negative = False
    if text[0] == "(" and text[-1] == ")":
        negative = True
        text = text[1:-1].strip()
    text = text.strip(_CURRENCY_SYMBOLS + " ")
    text = _CURRENCY_CODE_SUFFIX.sub("", _CURRENCY_CODE_PREFIX.sub("", text))
    text = text.strip(_CURRENCY_SYMBOLS + " ")
    if text[:1] in {"-", "+"}:
        negative = negative or text[0] == "-"
        text = text[1:].strip()
    elif text.endswith("-"):
        negative = True
        text = text[:-1].strip()
    if not _AMOUNT_BODY.fullmatch(text):
        return None
    split = _split_amount(text)
    if split is None:
        return None
    integer_digits, fraction = split
    try:
        amount = Decimal(f"{integer_digits or '0'}.{fraction or '0'}")
        minor = int(amount.quantize(MINOR_UNITS, rounding=ROUND_HALF_UP) * 100)
    except InvalidOperation:
        return None
    return -minor if negative else minor


# ---------------------------------------------------------------------------------------------
# compute_forms
# ---------------------------------------------------------------------------------------------


def compute_forms(value: str, field_class: FieldClass, forms: Sequence[str]) -> list[FormValue]:
    """The spec 8.4 forms of ``value``, in the order requested.

    ``forms`` lists form names (``raw``, ``digits.1``, ...) or the expanding ``digits`` and
    ``phonetic``; an empty sequence means :func:`default_forms` of ``field_class``. Forms that
    cannot be computed (an unparseable date, a non-numeric amount, a name token without a
    phonetic code) are left out, as are forms shorter than :data:`MIN_FORM_LEN` (phonetic codes
    excepted) and forms whose value repeats an earlier form's value in the same token domain.
    A form name that is not one of spec 8.4 raises :class:`ValueError`.
    """
    requests = [parse_form_request(name) for name in (tuple(forms) or default_forms(field_class))]
    raw = normalize(value)
    if len(raw) > MAX_FORM_VALUE_LEN:
        raw = normalize(raw[:MAX_FORM_VALUE_LEN])
    if not raw:
        return []

    out: list[FormValue] = []
    seen: set[tuple[TokenDomain, str]] = set()

    def emit(form: str, text: str, *, min_len: int = MIN_FORM_LEN) -> None:
        if len(text) < min_len:
            return
        key = (token_domain(form), text)
        if key in seen:
            return
        seen.add(key)
        out.append(FormValue(form, text))

    norm: str | None = None
    runs: list[str] | None = None
    codes: list[str] | None = None
    for base, index in requests:
        if base == "raw":
            emit("raw", raw)
        elif base == "norm":
            norm = raw.lower() if norm is None else norm
            emit("norm", norm)
        elif base == "alnum":
            norm = raw.lower() if norm is None else norm
            emit("alnum", "".join(char for char in norm if char.isalnum()))
        elif base == "digits":
            runs = digit_runs(raw) if runs is None else runs
            for k, run in enumerate(runs):
                if index is None or index == k:
                    emit(f"digits.{k}", run)
        elif base == "date":
            text = iso_date(raw)
            if text is not None:
                emit("date", text)
        elif base == "amount":
            minor = amount_minor_units(raw)
            if minor is not None:
                emit("amount", str(minor))
        else:  # phonetic
            codes = phonetic_codes(raw) if codes is None else codes
            for k, code in enumerate(codes):
                if code and (index is None or index == k):
                    emit(f"phonetic.{k}", code, min_len=1)
    return out
