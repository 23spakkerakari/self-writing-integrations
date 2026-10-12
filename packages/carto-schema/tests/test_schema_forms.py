"""Form names, token format and shapes (spec 8.3, 8.4)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from carto_schema.forms import (
    BASE_FORMS,
    DIGITS_MAX_K,
    FORM_PATTERN,
    PHONETIC_MAX_K,
    SHAPE_MAX_LEN,
    SHAPE_MAX_RUN,
    TOKEN_PATTERN,
    Form,
    Shape,
    Token,
    is_form,
    parse_form,
    shape,
    token_domain,
)

ALL_FORMS = [
    "raw",
    "norm",
    "alnum",
    "digits.0",
    "digits.1",
    "digits.2",
    "date",
    "amount",
    "phonetic.0",
    "phonetic.1",
    "phonetic.2",
    "phonetic.3",
    "phonetic.4",
    "phonetic.5",
    "phonetic.6",
    "phonetic.7",
]

NOT_FORMS = [
    "digits.3",
    "phonetic.8",
    "Raw",
    "",
    "digits",
    "phonetic",
    "digits.",
    "digits.-1",
    "digits.01",
    "digits.1.0",
    "amount.0",
    "raw.0",
    "DIGITS.0",
    " raw",
    "raw ",
    "raw\n",
    "norm\n",
    "alphanum",
    "phonetic.10",
]

VALID_TOKENS = [
    "t1.q8Jm0h3cR2VfZp4Lx9sT1w",
    "t2.Gk2Wq7nXf4Lr9bT0sYv3Ez",
    "t10.AAAAAAAAAAAAAAAAAAAAAA",
    "t999.0000000000000000000000",
    "t9999.-_-_-_-_-_-_-_-_-_-_-_",
]

INVALID_TOKENS = [
    "",
    "t1",
    "t1.",
    "q8Jm0h3cR2VfZp4Lx9sT1w",
    "t0.q8Jm0h3cR2VfZp4Lx9sT1w",
    "t01.q8Jm0h3cR2VfZp4Lx9sT1w",
    "t10000.q8Jm0h3cR2VfZp4Lx9sT1w",
    "T1.q8Jm0h3cR2VfZp4Lx9sT1w",
    "t1.q8Jm0h3cR2VfZp4Lx9sT1",
    "t1.q8Jm0h3cR2VfZp4Lx9sT1wX",
    "t1.q8Jm0h3cR2VfZp4Lx9sT1=",
    "t1.q8Jm0h3cR2VfZp4Lx9sT+w",
    "t1.q8Jm0h3cR2VfZp4Lx9sT/w",
    "t1.q8Jm0h3cR2VfZp4Lx9sT1w\n",
    " t1.q8Jm0h3cR2VfZp4Lx9sT1w",
    "t1_q8Jm0h3cR2VfZp4Lx9sT1w",
    "t-1.q8Jm0h3cR2VfZp4Lx9sT1w",
]


SHAPE_CASES = [
    ("SO-0004471", "AA-9999999"),
    ("88-210", "99-999"),
    ("X9-0442", "A9-9999"),
    ("9" * 20, "9" * 12 + "+"),
    ("c-88213", "A-99999"),
    ("SH-5521", "AA-9999"),
    ("SHIP_20260923_0200.csv", "AAAA_99999999_9999.AAA"),
    ("ab 12", "AA 99"),
    ("a\tb", "A\tA"),
    ("", ""),
    ("---", "---"),
    ("a" * 12, "A" * 12),
    ("a" * 13, "A" * 12 + "+"),
    ("-" * 13, "-" * 12 + "+"),
    ("x" * 20 + "1" * 20, "A" * 12 + "+" + "9" * 12 + "+"),
    ("1" * 13 + "a" + "1" * 13, "9" * 12 + "+A" + "9" * 12 + "+"),
    ("\u00dcn\u00efc\u00f6de-\u0661\u0662\u0663", "AAAAAAA-999"),
]


class _Carrier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    form: Form
    token: Token


class _ShapeCarrier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shape: Shape


@pytest.mark.parametrize("name", ALL_FORMS)
def test_every_form_name_is_accepted(name: str) -> None:
    assert is_form(name)
    assert FORM_PATTERN.fullmatch(name) is not None
    base, index = parse_form(name)
    assert base in {*BASE_FORMS, "digits", "phonetic"}
    assert (index is None) == (base in BASE_FORMS)


@pytest.mark.parametrize("name", NOT_FORMS)
def test_non_form_names_are_rejected(name: str) -> None:
    assert not is_form(name)
    with pytest.raises(ValueError, match="not a form name"):
        parse_form(name)
    with pytest.raises(ValueError, match="not a form name"):
        token_domain(name)


def test_parse_form_splits_base_and_index() -> None:
    assert parse_form("raw") == ("raw", None)
    assert parse_form("amount") == ("amount", None)
    assert parse_form("digits.0") == ("digits", 0)
    assert parse_form("digits.2") == ("digits", 2)
    assert parse_form("phonetic.7") == ("phonetic", 7)


def test_index_ranges_follow_the_spec() -> None:
    assert DIGITS_MAX_K == 2, "spec 8.4: digits.k has at most three runs"
    assert PHONETIC_MAX_K == 7
    assert all(is_form(f"digits.{k}") for k in range(DIGITS_MAX_K + 1))
    assert not is_form(f"digits.{DIGITS_MAX_K + 1}")
    assert all(is_form(f"phonetic.{k}") for k in range(PHONETIC_MAX_K + 1))
    assert not is_form(f"phonetic.{PHONETIC_MAX_K + 1}")
    assert len(ALL_FORMS) == len(BASE_FORMS) + (DIGITS_MAX_K + 1) + (PHONETIC_MAX_K + 1)


@pytest.mark.parametrize(
    ("name", "domain"),
    [
        ("raw", "id"),
        ("norm", "id"),
        ("alnum", "id"),
        ("digits.0", "id"),
        ("digits.1", "id"),
        ("digits.2", "id"),
        ("date", "date"),
        ("amount", "amt"),
        ("phonetic.0", "ph"),
        ("phonetic.7", "ph"),
    ],
)
def test_token_domain(name: str, domain: str) -> None:
    assert token_domain(name) == domain


def test_every_form_has_a_domain() -> None:
    domains = {token_domain(name) for name in ALL_FORMS}
    assert domains == {"id", "date", "amt", "ph"}


def test_raw_and_digits_share_the_id_domain() -> None:
    # Spec 8.4: '4471' from a raw field must match '4471' from a digits form.
    assert token_domain("raw") == token_domain("digits.0") == token_domain("alnum")


@pytest.mark.parametrize("name", ALL_FORMS)
def test_form_type_accepts_every_form(name: str) -> None:
    assert _Carrier(form=name, token=VALID_TOKENS[0]).form == name


@pytest.mark.parametrize("name", NOT_FORMS)
def test_form_type_rejects_non_forms(name: str) -> None:
    with pytest.raises(ValidationError) as info:
        _Carrier(form=name, token=VALID_TOKENS[0])
    assert info.value.errors()[0]["loc"] == ("form",)


@pytest.mark.parametrize("value", VALID_TOKENS)
def test_token_pattern_accepts(value: str) -> None:
    assert TOKEN_PATTERN.fullmatch(value) is not None
    assert _Carrier(form="raw", token=value).token == value


@pytest.mark.parametrize("value", INVALID_TOKENS)
def test_token_pattern_rejects(value: str) -> None:
    assert TOKEN_PATTERN.fullmatch(value) is None
    with pytest.raises(ValidationError) as info:
        _Carrier(form="raw", token=value)
    assert info.value.errors()[0]["loc"] == ("token",)


@pytest.mark.parametrize(("value", "expected"), SHAPE_CASES)
def test_shape(value: str, expected: str) -> None:
    assert shape(value) == expected


@pytest.mark.parametrize(("value", "expected"), SHAPE_CASES)
def test_shape_type_accepts_shapes_and_rejects_the_values_behind_them(
    value: str, expected: str
) -> None:
    if expected:
        assert _ShapeCarrier(shape=expected).shape == expected
    if value != expected:
        with pytest.raises(ValidationError) as info:
            _ShapeCarrier(shape=value)
        error = info.value.errors()[0]
        assert (error["type"], error["loc"]) == ("value_error", ("shape",))
        assert value not in error["msg"]


@pytest.mark.parametrize("value", ["", "A9" * 33, "9" * 13, "+" * 14, "a", "SO-0004471", "A-1"])
def test_shape_type_rejects(value: str) -> None:
    with pytest.raises(ValidationError) as info:
        _ShapeCarrier(shape=value)
    assert info.value.errors()[0]["loc"] == ("shape",)


def test_shape_type_accepts_the_longest_shape() -> None:
    assert _ShapeCarrier(shape="A9" * 32).shape == "A9" * 32
    assert _ShapeCarrier(shape="9" * 12 + "+").shape == "9" * 12 + "+"


def test_shape_is_cut_at_the_identifier_limit() -> None:
    long_shape = shape("A9" * 100)
    assert len(long_shape) == SHAPE_MAX_LEN
    assert long_shape == ("A9" * 100)[:SHAPE_MAX_LEN]


def test_shape_constants() -> None:
    assert SHAPE_MAX_LEN == 64
    assert SHAPE_MAX_RUN == 12


def test_shape_is_idempotent() -> None:
    for value in ["SO-0004471", "9" * 40, "+" * 30, "A9" * 100, "ab cd-ef_12.34"]:
        assert shape(shape(value)) == shape(value)
