"""carto_edge.pipeline.redact: Presidio on template constants (spec 8.3), attribute hygiene
(spec 7.1: values truncated to 256 chars, only non-sensitive values kept)."""

from __future__ import annotations

import base64
import json

import pytest

from carto_edge.pipeline.pii import PiiHit, RegexDetector
from carto_edge.pipeline.redact import (
    MAX_ATTRIBUTE_LEN,
    TEMPLATE_CACHE_SIZE,
    attribute_cache_clear,
    attribute_cache_info,
    attribute_is_clean,
    redact_template_text,
    template_cache_clear,
    template_cache_info,
    truncate_attribute,
)


class CountingDetector:
    def __init__(self) -> None:
        self.inner = RegexDetector()
        self.calls = 0

    def detect(self, text: str) -> list[PiiHit]:
        self.calls += 1
        return self.inner.detect(text)


def _jwt() -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return f"{b64(json.dumps({'alg': 'HS256'}).encode())}.{b64(b'{}')}.{b64(bytes(range(32)))}"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    template_cache_clear()


def test_template_constants_are_masked() -> None:
    detector = CountingDetector()
    text, masked = redact_template_text(
        "notify ops@example.org about order <*> ssn 123-45-6789 for <*>", detector
    )
    assert "ops@example.org" not in text
    assert "123-45-6789" not in text
    assert "<EMAIL_ADDRESS>" in text
    assert "<US_SSN>" in text
    assert "order <*>" in text
    assert masked == 2


def test_clean_template_is_unchanged() -> None:
    template = "order created from cart order_id=<*> cart_id=<*> total=<*>"
    assert redact_template_text(template, CountingDetector()) == (template, 0)
    assert redact_template_text("", CountingDetector()) == ("", 0)


def test_results_are_cached_per_template_text_and_detector() -> None:
    detector = CountingDetector()
    template = "payment requested for <*> contact ops@example.org"
    first = redact_template_text(template, detector)
    second = redact_template_text(template, detector)
    assert first == second
    assert detector.calls == 1
    other = CountingDetector()
    redact_template_text(template, other)
    assert other.calls == 1
    info = template_cache_info()
    assert info.hits == 1
    assert info.maxsize == TEMPLATE_CACHE_SIZE


def test_cache_is_bounded() -> None:
    detector = CountingDetector()
    for i in range(TEMPLATE_CACHE_SIZE + 10):
        redact_template_text(f"template number {i} <*>", detector)
    assert template_cache_info().currsize <= TEMPLATE_CACHE_SIZE


def test_long_template_text_is_truncated_before_detection() -> None:
    detector = CountingDetector()
    text, masked = redact_template_text("x" * 5000 + " ops@example.org", detector)
    assert masked == 0
    assert len(text.encode("utf-8")) <= 4096


@pytest.mark.parametrize("value", ["RELEASED", "DC-03", "us-east", "200", "", "   ", "v1.2.3"])
def test_attribute_is_clean_for_plain_values(value: str) -> None:
    assert attribute_is_clean(value, RegexDetector())


@pytest.mark.parametrize(
    "value",
    [
        "alex.chen@example.org",
        "123-45-6789",
        "4111 1111 1111 1111",
        "GB82WEST12345698765432",
        "+14155550123",
        _jwt(),
        "AK" + "IA" + "J7Q2M4X9P1L3N6R8",
    ],
)
def test_attribute_is_not_clean_for_pii_or_secrets(value: str) -> None:
    assert not attribute_is_clean(value, RegexDetector())


def test_attribute_is_clean_checks_the_truncated_value() -> None:
    value = "A" * MAX_ATTRIBUTE_LEN + " alex.chen@example.org"
    assert attribute_is_clean(value, RegexDetector())  # the e-mail falls beyond the cut
    assert not attribute_is_clean("alex.chen@example.org " + "A" * 300, RegexDetector())


def test_truncate_attribute() -> None:
    assert truncate_attribute("abc") == "abc"
    assert truncate_attribute("  padded  ") == "padded"
    assert len(truncate_attribute("x" * 1000)) == MAX_ATTRIBUTE_LEN
    assert MAX_ATTRIBUTE_LEN == 256
    assert truncate_attribute("é" * 300) == "é" * 256


class EmailDetector:
    """Hits on e-mail-looking text; counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, text: str) -> list[PiiHit]:
        self.calls += 1
        if "@" in text:
            return [PiiHit("EMAIL_ADDRESS", 0, len(text), 1.0)]
        return []


def test_attribute_verdicts_are_memoized_per_value_and_detector() -> None:
    attribute_cache_clear()
    detector = EmailDetector()
    for _ in range(1000):
        assert attribute_is_clean("us-east", detector)
        assert not attribute_is_clean("ops@example.com", detector)
    assert detector.calls == 2  # once per distinct value, not once per event
    info = attribute_cache_info()
    assert info.misses == 2 and info.hits == 1998
    other = EmailDetector()
    assert attribute_is_clean("us-east", other)
    assert other.calls == 1  # another detector never reuses a verdict
    assert attribute_is_clean("  us-east  ", detector)  # truncation first, then the cache
    assert detector.calls == 2
    attribute_cache_clear()
    assert attribute_cache_info().currsize == 0
