"""Access log parsing: combined and common formats, custom re2 patterns (spec 8.2 item 5)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.parse.access_log import (
    AccessLogParser,
    generalize_path,
    looks_like_access_log,
    status_class,
)

COMBINED = (
    '203.0.113.9 - alice [23/Sep/2026:13:04:06 +0000] "GET /orders/4471/items?x=1 HTTP/1.1" '
    '200 512 "https://shop.example/cart" "Mozilla/5.0 (X11; Linux)"'
)
COMMON = '10.0.0.5 - - [23/Sep/2026:13:04:06 -0400] "POST /api/v2/carts HTTP/1.1" 503 -'


def test_access_log_combined_format_fields_and_template() -> None:
    parsed = AccessLogParser().parse(COMBINED)
    assert parsed is not None
    assert parsed.fields == {
        "remote_addr": "203.0.113.9",
        "remote_user": "alice",
        "time": "23/Sep/2026:13:04:06 +0000",
        "method": "GET",
        "path": "/orders/4471/items?x=1",
        "protocol": "HTTP/1.1",
        "status": "200",
        "bytes": "512",
        "referer": "https://shop.example/cart",
        "user_agent": "Mozilla/5.0 (X11; Linux)",
    }
    assert parsed.template == "GET /orders/*/items 2xx"


def test_access_log_common_format_dashes_are_absent() -> None:
    parsed = AccessLogParser().parse(COMMON)
    assert parsed is not None
    assert parsed.fields == {
        "remote_addr": "10.0.0.5",
        "time": "23/Sep/2026:13:04:06 -0400",
        "method": "POST",
        "path": "/api/v2/carts",
        "protocol": "HTTP/1.1",
        "status": "503",
    }
    assert parsed.template == "POST /api/v*/carts 5xx"


def test_access_log_malformed_request_line_keeps_the_raw_request() -> None:
    parsed = AccessLogParser().parse('1.2.3.4 - - [23/Sep/2026:13:04:06 +0000] "-" 400 0')
    assert parsed is not None
    assert parsed.fields["request"] == "-"
    assert "method" not in parsed.fields
    assert parsed.template == "- - 4xx"


def test_access_log_rejects_other_lines() -> None:
    parser = AccessLogParser()
    assert parser.parse("") is None
    assert parser.parse("ts=1 level=info msg=x") is None
    assert parser.parse('{"a": 1}') is None
    assert parser.parse("2026-09-23 21:12:41 INFO PO export finished") is None


def test_access_log_custom_re2_pattern_with_named_groups() -> None:
    pattern = (
        r"^(?P<time>\S+) (?P<method>[A-Z]+) (?P<path>\S+) (?P<status>\d{3}) "
        r"(?P<duration_ms>\d+)ms$"
    )
    parser = AccessLogParser(pattern)
    parsed = parser.parse(
        "2026-09-23T13:04:06Z GET /u/7f3a9c2e-1b4d-4f0a-9c3e-2a1b4c5d6e7f/x 404 12ms"
    )
    assert parsed is not None
    assert parsed.fields == {
        "time": "2026-09-23T13:04:06Z",
        "method": "GET",
        "path": "/u/7f3a9c2e-1b4d-4f0a-9c3e-2a1b4c5d6e7f/x",
        "status": "404",
        "duration_ms": "12",
    }
    assert parsed.template == "GET /u/*/x 4xx"
    assert parser.parse("nope") is None


def test_access_log_custom_pattern_without_method_falls_back_to_dashes() -> None:
    parser = AccessLogParser(r"^(?P<ip>\S+) (?P<status>\d{3})$")
    parsed = parser.parse("1.2.3.4 200")
    assert parsed is not None
    assert parsed.template == "- - 2xx"


def test_access_log_invalid_custom_pattern_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="access_log_pattern"):
        AccessLogParser(r"(?P<a>x")
    with pytest.raises(ValueError, match="access_log_pattern"):
        AccessLogParser(r"(?P<a>x)\1")
    with pytest.raises(ValueError, match="named group"):
        AccessLogParser(r"^(\S+)$")


def test_access_log_custom_pattern_is_linear_time() -> None:
    parser = AccessLogParser(r"^(?P<a>(a+)+)$")
    assert parser.parse("a" * 40 + "b") is None


def test_generalize_path_rules() -> None:
    assert generalize_path("/orders/4471/items") == "/orders/*/items"
    assert generalize_path("/orders/4471?x=1&y=2") == "/orders/*"
    assert generalize_path("/u/7f3a9c2e-1b4d-4f0a-9c3e-2a1b4c5d6e7f/x") == "/u/*/x"
    assert generalize_path("/u/7F3A9C2E1B4D4F0A9C3E2A1B4C5D6E7F") == "/u/*"
    assert generalize_path("/api/v2/carts") == "/api/v*/carts"
    assert generalize_path("/static/app.js") == "/static/app.js"
    assert generalize_path("/") == "/"
    assert generalize_path("") == "-"
    assert generalize_path("/a/b#frag") == "/a/b"


def test_generalize_path_is_bounded() -> None:
    assert len(generalize_path("/" + "x" * 5000)) <= 512


def test_status_class_rules() -> None:
    assert status_class("200") == "2xx"
    assert status_class("404") == "4xx"
    assert status_class("503") == "5xx"
    assert status_class("-") == "-"
    assert status_class("abc") == "-"
    assert status_class("") == "-"


def test_looks_like_access_log_is_a_cheap_check() -> None:
    assert looks_like_access_log(COMBINED)
    assert looks_like_access_log(COMMON)
    assert not looks_like_access_log("ts=1 level=info")
    assert not looks_like_access_log("")


@settings(max_examples=200, deadline=2000)
@given(st.text(max_size=300))
def test_access_log_property_never_raises(text: str) -> None:
    result = AccessLogParser().parse(text)
    assert result is None or isinstance(result.template, str)
    generalize_path(text)
