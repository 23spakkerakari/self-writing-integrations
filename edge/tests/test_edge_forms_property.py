"""Spec 18.1 properties for forms and tokens: normalization is idempotent, forms are
deterministic, tokens are deterministic per key version and differ across versions and domains.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from carto_common.crypto import Keyring, TokenKey, generate_key, token
from carto_edge.pipeline.forms import MIN_FORM_LEN, compute_forms, normalize, parse_form_request
from carto_edge.pipeline.model import FieldClass
from carto_edge.pipeline.tokenize import Tokenizer
from carto_schema.forms import TOKEN_PATTERN, is_form, token_domain

KEY_A = generate_key()
KEY_B = generate_key()
KEYRING = Keyring(active=TokenKey(2, KEY_B), previous=(TokenKey(1, KEY_A),))
ALL_FORMS = ("raw", "norm", "alnum", "digits", "date", "amount", "phonetic")
DOMAINS = ("id", "date", "amt", "ph")

values = st.text(min_size=0, max_size=80)
identifier_like = st.from_regex(r"\A[A-Za-z]{0,4}[-_ ]?[0-9]{3,12}\Z")


@given(values)
def test_normalization_is_idempotent(value: str) -> None:
    once = normalize(value)
    assert normalize(once) == once


@given(values, st.sampled_from(list(FieldClass)))
def test_forms_are_deterministic_and_well_formed(value: str, cls: FieldClass) -> None:
    first = compute_forms(value, cls, ALL_FORMS)
    assert compute_forms(value, cls, ALL_FORMS) == first
    names = [entry.form for entry in first]
    assert len(set(names)) == len(names)
    seen: set[tuple[str, str]] = set()
    for entry in first:
        assert is_form(entry.form)
        assert entry.value
        if not entry.form.startswith("phonetic."):
            assert len(entry.value) >= MIN_FORM_LEN
        key = (token_domain(entry.form), entry.value)
        assert key not in seen
        seen.add(key)


@given(values)
def test_raw_form_is_a_fixed_point(value: str) -> None:
    forms = compute_forms(value, FieldClass.IDENTIFIER, ("raw",))
    if forms:
        raw = forms[0].value
        assert compute_forms(raw, FieldClass.IDENTIFIER, ("raw",))[0].value == raw


@given(values, st.sampled_from(DOMAINS))
def test_tokens_are_deterministic_per_key_version(value: str, domain: str) -> None:
    first = token(KEY_A, 1, domain, value)  # type: ignore[arg-type]
    assert token(KEY_A, 1, domain, value) == first  # type: ignore[arg-type]
    assert TOKEN_PATTERN.fullmatch(first)
    assert first.startswith("t1.")


@given(values, st.sampled_from(DOMAINS))
def test_tokens_differ_across_key_versions(value: str, domain: str) -> None:
    under_a = token(KEY_A, 1, domain, value)  # type: ignore[arg-type]
    under_b = token(KEY_B, 2, domain, value)  # type: ignore[arg-type]
    assert under_a != under_b
    assert under_a.partition(".")[2] != under_b.partition(".")[2]


@given(values)
def test_tokens_differ_across_domains(value: str) -> None:
    bodies = {token(KEY_A, 1, domain, value).partition(".")[2] for domain in DOMAINS}  # type: ignore[arg-type]
    assert len(bodies) == len(DOMAINS)


@settings(max_examples=60)
@given(identifier_like)
def test_tokenizer_matches_token_pattern_for_every_key(value: str) -> None:
    tokenizer = Tokenizer(KEYRING, "default")
    tokens = tokenizer.tokenize_query(value)
    assert all(TOKEN_PATTERN.fullmatch(item) for item in tokens)
    versions = {Keyring.version_of(item) for item in tokens}
    assert versions <= {1, 2}
    if tokens:
        assert tokens == tokenizer.tokenize_query(value)


@given(st.sampled_from(ALL_FORMS))
def test_form_request_expansion_is_stable(name: str) -> None:
    assert parse_form_request(name) == parse_form_request(name)
