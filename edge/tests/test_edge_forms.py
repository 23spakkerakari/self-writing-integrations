"""carto_edge.pipeline.forms: the spec 8.4 forms table, worked example and edge cases."""

from __future__ import annotations

import pytest

from carto_edge.pipeline.forms import (
    FormValue,
    amount_minor_units,
    compute_forms,
    default_forms,
    iso_date,
    normalize,
)
from carto_edge.pipeline.model import FieldClass
from carto_schema.forms import DIGITS_MAX_K, PHONETIC_MAX_K, is_form

ID_FORMS = ("raw", "norm", "alnum", "digits")


def forms_of(
    value: str, forms: tuple[str, ...] = ID_FORMS, cls: FieldClass = FieldClass.IDENTIFIER
) -> dict[str, str]:
    return {entry.form: entry.value for entry in compute_forms(value, cls, forms)}


# ---------------------------------------------------------------------------------------------
# Worked example (spec 8.4 table)
# ---------------------------------------------------------------------------------------------


def test_spec_worked_example_so_0004471() -> None:
    result = compute_forms("SO-0004471", FieldClass.IDENTIFIER, ID_FORMS)
    assert result == [
        FormValue("raw", "SO-0004471"),
        FormValue("norm", "so-0004471"),
        FormValue("alnum", "so0004471"),
        FormValue("digits.0", "4471"),
    ]


def test_every_form_name_is_a_schema_form() -> None:
    value = "Jane Smith 12345 SO-0004471 2026-10-06"
    forms = ("raw", "norm", "alnum", "digits", "phonetic")
    for entry in compute_forms(value, FieldClass.PERSON_NAME, forms):
        assert is_form(entry.form)


def test_date_form_from_spec_example() -> None:
    assert forms_of("2026-10-06", ("date",), FieldClass.DATE) == {"date": "2026-10-06"}


def test_amount_form_from_spec_example() -> None:
    assert forms_of("1299.99", ("amount",), FieldClass.AMOUNT) == {"amount": "129999"}


def test_phonetic_form_from_spec_example() -> None:
    result = forms_of("Jane Smith", ("phonetic",), FieldClass.PERSON_NAME)
    assert result == {"phonetic.0": "JN", "phonetic.1": "SM0"}


# ---------------------------------------------------------------------------------------------
# raw / norm / alnum
# ---------------------------------------------------------------------------------------------


def test_raw_is_nfkc_and_trimmed() -> None:
    # fullwidth letters and digits, a non-breaking space, surrounding whitespace
    assert normalize("  \uff33\uff2f-\uff10\uff10\uff14\u00a0 ") == "SO-004"
    assert normalize("\u2460") == "1"  # circled digit one


def test_normalize_is_idempotent_on_examples() -> None:
    for value in ("  SO-0004471 ", "\uff33O", "\ufb01le", "\uff21\u00a0B", "x\u0301"):
        once = normalize(value)
        assert normalize(once) == once


def test_norm_is_lowercase_of_raw() -> None:
    assert forms_of("AbC-123X")["norm"] == "abc-123x"


def test_alnum_strips_non_alphanumerics() -> None:
    assert forms_of("ab_c.1-2 3/x")["alnum"] == "abc123x"


# ---------------------------------------------------------------------------------------------
# digits.k
# ---------------------------------------------------------------------------------------------


def test_digit_runs_need_four_or_more_digits() -> None:
    result = forms_of("A-123-B-4567-C-89")
    assert result.get("digits.0") == "4567"
    assert "digits.1" not in result


def test_leading_zeros_stripped_and_short_results_skipped() -> None:
    result = forms_of("X-0000-Y-00012-Z-000456")
    # run 0 "0000" collapses to "" and run 1 "00012" to "12": both below 3 characters
    assert "digits.0" not in result
    assert "digits.1" not in result
    assert result["digits.2"] == "456"


def test_at_most_three_digit_runs() -> None:
    result = forms_of("1111-2222-3333-4444-5555")
    assert [form for form in result if form.startswith("digits.")] == [
        f"digits.{k}" for k in range(DIGITS_MAX_K + 1)
    ]
    assert result["digits.2"] == "3333"


def test_specific_digit_run_can_be_requested() -> None:
    result = forms_of("1111-2222-3333", ("digits.1",))
    assert result == {"digits.1": "2222"}


def test_unicode_digits_are_normalized_before_run_detection() -> None:
    assert forms_of("\uff11\uff12\uff13\uff14", ("digits",)) == {"digits.0": "1234"}


# ---------------------------------------------------------------------------------------------
# minimum length and duplicate suppression
# ---------------------------------------------------------------------------------------------


def test_forms_shorter_than_three_characters_are_skipped() -> None:
    assert forms_of("A-") == {}
    assert forms_of("ab") == {}
    assert forms_of("a-b") == {"raw": "a-b"}  # alnum "ab" is too short, norm is a duplicate


def test_duplicate_values_are_not_repeated() -> None:
    # raw, norm, alnum and digits.0 all equal "4471"; only raw is kept.
    assert compute_forms("4471", FieldClass.IDENTIFIER, ID_FORMS) == [FormValue("raw", "4471")]


def test_duplicate_suppression_is_per_domain() -> None:
    # The same text in the id and the date domain tokenizes differently; both are kept.
    result = forms_of("2026-10-06", ("raw", "date"), FieldClass.DATE)
    assert result == {"raw": "2026-10-06", "date": "2026-10-06"}


def test_whitespace_only_and_empty_values_give_nothing() -> None:
    assert compute_forms("", FieldClass.IDENTIFIER, ID_FORMS) == []
    assert compute_forms("   \t ", FieldClass.IDENTIFIER, ID_FORMS) == []


def test_unknown_form_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="form"):
        compute_forms("abc", FieldClass.IDENTIFIER, ("soundex",))


def test_empty_forms_fall_back_to_the_class_defaults() -> None:
    assert default_forms(FieldClass.IDENTIFIER) == ("raw", "norm", "alnum", "digits")
    assert default_forms(FieldClass.DATE) == ("date",)
    assert default_forms(FieldClass.AMOUNT) == ("amount",)
    assert default_forms(FieldClass.SECRET_LIKE) == ()
    assert forms_of("SO-0004471", ()) == forms_of("SO-0004471", ID_FORMS)
    assert compute_forms("hunter2x", FieldClass.SECRET_LIKE, ()) == []


# ---------------------------------------------------------------------------------------------
# date
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-06", "2026-10-06"),
        ("2026-10-06T21:12:03Z", "2026-10-06"),
        ("2026-10-06T23:30:00-02:00", "2026-10-07"),  # UTC date of an offset time
        ("2026-10-06 21:12:03.412", "2026-10-06"),
        ("20261006", "2026-10-06"),
        ("2026/10/06", "2026-10-06"),
        ("06.10.2026", "2026-10-06"),
        ("10/06/2026", "2026-10-06"),  # month first when both parts could be a month
        ("25/10/2026", "2026-10-25"),  # day first when the first part cannot be a month
        ("06-Oct-2026", "2026-10-06"),
        ("Oct 6, 2026", "2026-10-06"),
        ("6 October 2026", "2026-10-06"),
        ("Tue, 06 Oct 2026 21:12:03 +0000", "2026-10-06"),
        ("1791321123", "2026-10-06"),  # epoch seconds (2026-10-06T21:12:03Z)
        ("1791321123412", "2026-10-06"),  # epoch milliseconds
    ],
)
def test_iso_date_parses_common_formats(text: str, expected: str) -> None:
    assert iso_date(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "abc", "2026-13-01", "2026-02-30", "32/10/2026", "99999999", "12:30:00", "1234", "x" * 80],
)
def test_iso_date_rejects_nonsense(text: str) -> None:
    assert iso_date(text) is None


def test_date_form_is_skipped_for_unparseable_values() -> None:
    assert forms_of("not a date", ("date",), FieldClass.DATE) == {}


# ---------------------------------------------------------------------------------------------
# amount
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1299.99", 129999),
        ("1299", 129900),
        ("1,299.99", 129999),
        ("$1,299.99", 129999),
        ("EUR 1.299,99", 129999),
        ("1 299,99", 129999),
        ("12,99", 1299),
        ("1,299", 129900),
        ("(1,299.99)", -129999),
        ("-0.5", -50),
        ("+7", 700),
        (".99", 99),
        ("1299.99 USD", 129999),
        ("1.005", 101),  # three fraction digits round half up to minor units
        ("1'299.99", 129999),
        ("0", 0),
    ],
)
def test_amount_minor_units(text: str, expected: int) -> None:
    assert amount_minor_units(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1,2345", "1.2.3", "12-34", "1e5", "--5", "$", "1,,2"])
def test_amount_rejects_non_numeric(text: str) -> None:
    assert amount_minor_units(text) is None


def test_amount_form_value_is_the_minor_units_text() -> None:
    assert forms_of("$1,299.99", ("amount",), FieldClass.AMOUNT) == {"amount": "129999"}
    assert forms_of("(10)", ("amount",), FieldClass.AMOUNT) == {"amount": "-1000"}


def test_amount_form_shorter_than_three_characters_is_skipped() -> None:
    assert forms_of("0.05", ("amount",), FieldClass.AMOUNT) == {}


# ---------------------------------------------------------------------------------------------
# phonetic.k
# ---------------------------------------------------------------------------------------------


def test_phonetic_skips_tokens_without_a_code_and_keeps_positions() -> None:
    result = forms_of("Jane 123 Smith", ("phonetic",), FieldClass.PERSON_NAME)
    assert result == {"phonetic.0": "JN", "phonetic.2": "SM0"}


def test_phonetic_codes_may_be_shorter_than_three_characters() -> None:
    # Double Metaphone codes are at most four characters and often two (spec 8.4 example "JN").
    assert forms_of("Lee", ("phonetic",), FieldClass.PERSON_NAME) == {"phonetic.0": "L"}


def test_phonetic_caps_the_number_of_name_tokens() -> None:
    names = " ".join(f"Smith{i}" for i in range(PHONETIC_MAX_K + 5))
    result = forms_of(names, ("phonetic",), FieldClass.PERSON_NAME)
    assert len(result) == 1  # every token has the same code; later ones are duplicates
    many = " ".join(["Jane", "Smith", "Brown", "Miller", "Garcia", "Lopez", "Nguyen", "Kim", "Wu"])
    result = forms_of(many, ("phonetic",), FieldClass.PERSON_NAME)
    assert max(int(form.split(".")[1]) for form in result) <= PHONETIC_MAX_K


def test_phonetic_is_case_insensitive() -> None:
    assert forms_of("JANE smith", ("phonetic",), FieldClass.PERSON_NAME) == forms_of(
        "Jane Smith", ("phonetic",), FieldClass.PERSON_NAME
    )


def test_specific_phonetic_index_can_be_requested() -> None:
    assert forms_of("Jane Smith", ("phonetic.1",), FieldClass.PERSON_NAME) == {"phonetic.1": "SM0"}


# ---------------------------------------------------------------------------------------------
# determinism and idempotence on examples (properties live in test_edge_forms_property.py)
# ---------------------------------------------------------------------------------------------


def test_compute_forms_of_the_raw_form_gives_the_same_raw_form() -> None:
    for value in ("  SO-0004471 ", "Jane\u00a0Smith", "\uff11\uff12\uff13\uff14"):
        raw = compute_forms(value, FieldClass.IDENTIFIER, ("raw",))[0].value
        assert compute_forms(raw, FieldClass.IDENTIFIER, ("raw",))[0].value == raw


def test_compute_forms_is_deterministic() -> None:
    value = "Order SO-0004471 for Jane Smith on 2026-10-06"
    forms = ("raw", "norm", "alnum", "digits", "phonetic")
    first = compute_forms(value, FieldClass.PERSON_NAME, forms)
    assert compute_forms(value, FieldClass.PERSON_NAME, forms) == first
