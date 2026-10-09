"""carto_edge.pipeline.pii: Presidio with an explicit offline registry, the regex fallback, masking.

The Presidio engine (spaCy ``en_core_web_sm``) loads once per module through a module-scoped
fixture; every other test runs on the regex detector. All text is synthetic. Secret-shaped
literals are assembled at runtime so no secret scanner trips on the source tree.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import time
import warnings
from collections.abc import Iterator

import pytest
import structlog
import tldextract
from presidio_analyzer.predefined_recognizers import EmailRecognizer, UrlRecognizer

from carto_edge.config import ClassifySettings, PiiSettings
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import FieldClass, Policy
from carto_edge.pipeline.pii import (
    MAX_TEXT_BYTES,
    PRESIDIO_ENTITIES,
    PiiHit,
    PresidioDetector,
    RegexDetector,
    build_registry,
    detect_many,
    detector_from_settings,
    looks_like_secret,
    mask,
    truncate_text,
)
from carto_edge.pipeline.stats import FieldStatsStore

PRESIDIO_LOAD_SECONDS: float = 0.0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"sub": "svc-orders", "iat": 1_700_000_000}).encode())
    return f"{header}.{claims}.{_b64url(bytes(range(32)))}"


def _aws_key() -> str:
    return "AK" + "IA" + "J7Q2M4X9P1L3N6R8"


def _pem() -> str:
    body = _b64url(bytes(range(48)))
    return f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"


def _high_entropy() -> str:
    raw = bytes([13, 77, 201, 5, 99, 180, 33, 250, 61, 142, 7, 88, 219, 4, 170, 46] * 2)
    return "Qx7" + _b64url(bytes(byte ^ (index * 37 % 256) for index, byte in enumerate(raw)))


# ---------------------------------------------------------------------------------------------
# Presidio (loaded once per module)
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def presidio() -> Iterator[PresidioDetector]:
    """Load the real engine once; spaCy and Presidio may warn on load under ``-W error``."""
    global PRESIDIO_LOAD_SECONDS  # noqa: PLW0603 - recorded for the milestone report
    detector = PresidioDetector(PiiSettings())
    started = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", FutureWarning)
        warnings.simplefilter("ignore", UserWarning)
        detector.load()
    PRESIDIO_LOAD_SECONDS = time.perf_counter() - started
    assert detector.backend == "presidio"
    yield detector


def test_presidio_finds_person_email_and_ssn(presidio: PresidioDetector) -> None:
    text = "Contact Maria Lopez at maria.lopez@example.com, SSN 412-63-5174"
    hits = presidio.detect(text)
    found = {hit.entity_type for hit in hits}
    assert {"PERSON", "EMAIL_ADDRESS", "US_SSN"} <= found
    masked, count = mask(text, hits)
    assert count == len(hits) >= 3
    assert "maria.lopez@example.com" not in masked
    assert "412-63-5174" not in masked
    assert "Maria Lopez" not in masked
    assert "<EMAIL_ADDRESS>" in masked
    assert "<US_SSN>" in masked
    assert "<PERSON>" in masked


def test_presidio_registry_has_no_url_recognizer_and_no_tldextract_path(
    presidio: PresidioDetector, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = presidio.registry
    assert registry is not None
    names = {type(recognizer).__name__ for recognizer in registry.recognizers}
    assert "UrlRecognizer" not in names
    assert not any(isinstance(r, UrlRecognizer) for r in registry.recognizers)
    # The stock e-mail recognizer validates through tldextract, which fetches the public
    # suffix list from the internet on first use (spec 2.3 invariant 4). Ours must not.
    for recognizer in registry.recognizers:
        if isinstance(recognizer, EmailRecognizer):
            assert type(recognizer).validate_result is not EmailRecognizer.validate_result

    def boom(*_args: object, **_kwargs: object) -> object:
        msg = "tldextract must never be called by the edge"
        raise AssertionError(msg)

    monkeypatch.setattr(tldextract, "extract", boom)
    hits = presidio.detect("mail to alex.chen@example.org today")
    assert any(hit.entity_type == "EMAIL_ADDRESS" for hit in hits)


def test_presidio_supported_entities_are_exactly_the_configured_ones(
    presidio: PresidioDetector,
) -> None:
    registry = presidio.registry
    assert registry is not None
    supported: set[str] = set()
    for recognizer in registry.recognizers:
        supported.update(recognizer.supported_entities)
    assert supported == set(PRESIDIO_ENTITIES)


def test_presidio_truncates_input_to_four_kilobytes(presidio: PresidioDetector) -> None:
    text = ("x" * (MAX_TEXT_BYTES + 10)) + " alex.chen@example.org"
    hits = presidio.detect(text)
    assert all(hit.end <= MAX_TEXT_BYTES for hit in hits)
    assert not any(hit.entity_type == "EMAIL_ADDRESS" for hit in hits)


def test_presidio_scores_respect_threshold(presidio: PresidioDetector) -> None:
    # A bare ten-digit number is a weak phone candidate (score 0.4) and stays below 0.5.
    assert not [hit for hit in presidio.detect("4155550123") if hit.score >= 0.5]
    assert all(hit.score >= PiiSettings().score_threshold for hit in presidio.detect("Maria Lopez"))


def test_presidio_detect_emits_no_library_warnings(
    presidio: PresidioDetector, caplog: pytest.LogCaptureFixture
) -> None:
    """spaCy labels the registry does not use (CARDINAL, DATE, ORG, ...) are configured as
    ignored; otherwise Presidio logs a warning per unmapped entity on every call."""
    with caplog.at_level(logging.WARNING, logger="presidio-analyzer"):
        presidio.detect("Invoice 4471 for Maria Lopez on 6 October 2026, 3 items, ACME Corp")
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_presidio_load_is_idempotent_and_fast_enough(presidio: PresidioDetector) -> None:
    before = presidio.registry
    presidio.load()
    assert presidio.registry is before
    assert PRESIDIO_LOAD_SECONDS < 60.0


def test_classifier_with_presidio_finds_names_in_unnamed_field(
    presidio: PresidioDetector,
) -> None:
    settings = ClassifySettings(sample_values=32, quarantine_samples=20)
    classifier = Classifier(settings, (), presidio, FieldStatsStore.from_settings(settings))
    ref = "sys_web/tpl_000000000001/msg.param_0"
    firsts = ["Maria", "Alex", "Priya", "Jonas", "Amara", "Luis", "Hannah", "Omar"]
    lasts = ["Lopez", "Chen", "Patel", "Weber", "Okafor", "Garcia", "Schmidt", "Haddad"]
    for i in range(64):
        classifier.observe(ref, "msg.param_0", f"{firsts[i % 8]} {lasts[(i * 3) % 8]}")
    decision = classifier.decide(ref, "msg.param_0")
    assert decision.field_class is FieldClass.PERSON_NAME
    assert decision.policy is Policy.DROP
    assert decision.pinned is False


# ---------------------------------------------------------------------------------------------
# Fallbacks and the factory
# ---------------------------------------------------------------------------------------------


def test_factory_returns_regex_detector_when_disabled() -> None:
    with structlog.testing.capture_logs() as logs:
        detector = detector_from_settings(PiiSettings(enabled=False))
    assert isinstance(detector, RegexDetector)
    assert any(
        entry["event"] == "pii_detector_fallback" and entry["log_level"] == "warning"
        for entry in logs
    )


def test_factory_returns_presidio_detector_when_enabled() -> None:
    detector = detector_from_settings(PiiSettings())
    assert isinstance(detector, PresidioDetector)
    assert detector.backend == "unloaded"  # lazy: nothing loads until the first detect


def test_presidio_falls_back_to_regex_when_the_model_cannot_load() -> None:
    detector = PresidioDetector(PiiSettings(spacy_model="no_such_model_for_carto_tests"))
    with structlog.testing.capture_logs() as logs:
        hits = detector.detect("mail alex.chen@example.org now")
    assert detector.backend == "regex"
    assert any(hit.entity_type == "EMAIL_ADDRESS" for hit in hits)
    assert any(
        entry["event"] == "pii_detector_fallback" and entry["log_level"] == "warning"
        for entry in logs
    )
    # The warning is logged once; later calls keep using the fallback silently.
    with structlog.testing.capture_logs() as logs:
        detector.detect("again alex.chen@example.org")
    assert not logs


# ---------------------------------------------------------------------------------------------
# RegexDetector
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def regex() -> RegexDetector:
    return RegexDetector()


def test_regex_email(regex: RegexDetector) -> None:
    hits = regex.detect("send to alex.chen+orders@example.co.uk please")
    assert [hit.entity_type for hit in hits] == ["EMAIL_ADDRESS"]
    assert hits[0].score >= 0.5


@pytest.mark.parametrize(
    "text",
    ["call +14155550123 now", "call (415) 555-0123", "call 415-555-0123", "tel +44 20 7946 0958"],
)
def test_regex_phone(regex: RegexDetector, text: str) -> None:
    assert any(hit.entity_type == "PHONE_NUMBER" for hit in regex.detect(text))


def test_regex_phone_ignores_short_numbers_and_order_ids(regex: RegexDetector) -> None:
    assert not regex.detect("order 4471 created")
    assert not regex.detect("ref SO-0004471 total 129.99")


def test_regex_ssn(regex: RegexDetector) -> None:
    assert [hit.entity_type for hit in regex.detect("ssn 123-45-6789")] == ["US_SSN"]
    assert not regex.detect("ssn 000-45-6789")
    assert not regex.detect("ssn 666-45-6789")
    assert not regex.detect("ssn 912-45-6789")


def test_regex_credit_card_requires_luhn(regex: RegexDetector) -> None:
    assert [hit.entity_type for hit in regex.detect("card 4111 1111 1111 1111")] == ["CREDIT_CARD"]
    assert [hit.entity_type for hit in regex.detect("card 4111-1111-1111-1111")] == ["CREDIT_CARD"]
    assert not regex.detect("card 4111 1111 1111 1112")


def test_regex_iban_requires_checksum(regex: RegexDetector) -> None:
    assert [hit.entity_type for hit in regex.detect("iban GB82WEST12345698765432")] == ["IBAN_CODE"]
    assert [hit.entity_type for hit in regex.detect("iban DE89 3704 0044 0532 0130 00")] == [
        "IBAN_CODE"
    ]
    assert not regex.detect("iban GB82WEST12345698765433")


def test_regex_hits_are_sorted_and_non_overlapping(regex: RegexDetector) -> None:
    text = "alex.chen@example.org 123-45-6789 4111111111111111"
    hits = regex.detect(text)
    assert hits == sorted(hits, key=lambda hit: hit.start)
    for earlier, later in itertools.pairwise(hits):
        assert earlier.end <= later.start


def test_regex_ignores_clean_values(regex: RegexDetector) -> None:
    for value in ["RELEASED", "DC-03", "2026-10-06T21:12:03Z", "129.99", "c-88213", "X9-0442"]:
        assert regex.detect(value) == []


def test_regex_truncates_like_presidio(regex: RegexDetector) -> None:
    text = ("y" * (MAX_TEXT_BYTES + 5)) + " alex.chen@example.org"
    assert regex.detect(text) == []


# ---------------------------------------------------------------------------------------------
# Helpers: truncate, mask, detect_many, looks_like_secret
# ---------------------------------------------------------------------------------------------


def test_truncate_text_is_byte_precise_and_never_splits_a_character() -> None:
    assert truncate_text("abc") == "abc"
    long_ascii = "a" * (MAX_TEXT_BYTES + 1)
    assert len(truncate_text(long_ascii).encode("utf-8")) == MAX_TEXT_BYTES
    multibyte = "é" * MAX_TEXT_BYTES  # two bytes each
    cut = truncate_text(multibyte)
    assert len(cut.encode("utf-8")) <= MAX_TEXT_BYTES
    assert cut == "é" * (MAX_TEXT_BYTES // 2)


def test_mask_replaces_hits_and_merges_overlaps() -> None:
    text = "name Maria Lopez mail m@example.org"
    hits = [
        PiiHit("PERSON", 5, 16, 0.85),
        PiiHit("PERSON", 11, 16, 0.6),  # overlaps the first: merged, counted once
        PiiHit("EMAIL_ADDRESS", 22, 35, 1.0),
    ]
    masked, count = mask(text, hits)
    assert masked == "name <PERSON> mail <EMAIL_ADDRESS>"
    assert count == 2
    assert mask("nothing here", []) == ("nothing here", 0)


def test_mask_ignores_hits_outside_the_text() -> None:
    masked, count = mask("abc", [PiiHit("PERSON", 10, 20, 0.9)])
    assert (masked, count) == ("abc", 0)


def test_detect_many_attributes_hits_to_the_right_sample(regex: RegexDetector) -> None:
    samples = ["RELEASED", "alex.chen@example.org", "DC-03", "123-45-6789", "x" * 3000, "4471"]
    per_sample = detect_many(regex, samples)
    assert len(per_sample) == len(samples)
    assert per_sample[0] == []
    assert [hit.entity_type for hit in per_sample[1]] == ["EMAIL_ADDRESS"]
    assert per_sample[2] == []
    assert [hit.entity_type for hit in per_sample[3]] == ["US_SSN"]
    assert per_sample[5] == []
    assert detect_many(regex, []) == []


def test_detect_many_handles_more_text_than_one_call_allows(regex: RegexDetector) -> None:
    samples = [f"user{i}@example.org" for i in range(400)]  # about 8 KB in total
    per_sample = detect_many(regex, samples)
    assert all(hits and hits[0].entity_type == "EMAIL_ADDRESS" for hits in per_sample)


@pytest.mark.parametrize("value", [_jwt(), _aws_key(), _pem(), _high_entropy()])
def test_looks_like_secret_positive(value: str) -> None:
    assert looks_like_secret(value)
    assert looks_like_secret(f"prefix {value} suffix")


@pytest.mark.parametrize(
    "value",
    [
        "SO-0004471",
        "RELEASED",
        "c-88213",
        "0b1d9f2e-3c4a-4d5e-8f6a-7b8c9d0e1f2a",  # UUID: hex only, an identifier
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",  # ULID: no lower case, shorter than 32
        "a" * 64,  # long but no entropy
        "ORD-2026-10-08-ABCDEF0123456789-XYZ0001",  # no lower case letters
        "2026-10-06T21:12:03.412Z",
        "",
    ],
)
def test_looks_like_secret_negative(value: str) -> None:
    assert not looks_like_secret(value)


def test_build_registry_contains_the_expected_recognizers() -> None:
    registry = build_registry("en")
    names = sorted(type(recognizer).__name__ for recognizer in registry.recognizers)
    assert "UrlRecognizer" not in names
    assert "SpacyRecognizer" in names
    assert "UsSsnRecognizer" in names
    assert "CreditCardRecognizer" in names
    assert "IbanRecognizer" in names
    assert "PhoneRecognizer" in names
