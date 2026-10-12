"""Severity mapping: level fields, HTTP status classes, error lexicon (spec 8.2)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.severity import (
    LEVEL_FIELDS,
    STATUS_FIELDS,
    detect_severity,
    error_lexicon_matches,
    map_level,
    severity_from_status,
)
from carto_schema.event import Severity


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("info", Severity.INFO),
        ("INFO", Severity.INFO),
        ("Information", Severity.INFO),
        ("notice", Severity.INFO),
        ("warn", Severity.WARN),
        ("WARNING", Severity.WARN),
        ("error", Severity.ERROR),
        ("err", Severity.ERROR),
        ("ERR", Severity.ERROR),
        ("severe", Severity.ERROR),
        ("fatal", Severity.FATAL),
        ("critical", Severity.FATAL),
        ("crit", Severity.FATAL),
        ("emerg", Severity.FATAL),
        ("emergency", Severity.FATAL),
        ("alert", Severity.FATAL),
        ("panic", Severity.FATAL),
        ("debug", Severity.DEBUG),
        ("fine", Severity.DEBUG),
        ("trace", Severity.TRACE),
        ("finest", Severity.TRACE),
        ("verbose", Severity.TRACE),
        (" info ", Severity.INFO),
        ("[INFO]", Severity.INFO),
        ("INFO:", Severity.INFO),
        ("I", Severity.INFO),
        ("W", Severity.WARN),
        ("E", Severity.ERROR),
        ("D", Severity.DEBUG),
        ("F", Severity.FATAL),
        ("0", Severity.FATAL),
        ("1", Severity.FATAL),
        ("2", Severity.FATAL),
        ("3", Severity.ERROR),
        ("4", Severity.WARN),
        ("5", Severity.INFO),
        ("6", Severity.INFO),
        ("7", Severity.DEBUG),
        ("8", None),
        ("", None),
        ("banana", None),
        ("200", None),
        ("x" * 1000, None),
    ],
)
def test_severity_map_level(value: str, expected: Severity | None) -> None:
    assert map_level(value) == expected


def test_severity_from_status_classes() -> None:
    assert severity_from_status("503") == Severity.ERROR
    assert severity_from_status("500") == Severity.ERROR
    assert severity_from_status("404") == Severity.WARN
    assert severity_from_status("200") is None
    assert severity_from_status("302") is None
    assert severity_from_status("AUTHORIZED") is None
    assert severity_from_status("") is None
    assert severity_from_status("5030") is None
    assert severity_from_status("-") is None


def test_severity_error_lexicon_is_word_bounded_and_case_insensitive() -> None:
    assert error_lexicon_matches("SFTP upload failed: Permission denied <*>")
    assert error_lexicon_matches("Connection REFUSED by host")
    assert error_lexicon_matches("request timeout after <*>")
    assert error_lexicon_matches("NullPointerException thrown") is False
    assert error_lexicon_matches("an exception thrown")
    assert error_lexicon_matches("unauthorized access")
    assert error_lexicon_matches("forbidden")
    assert error_lexicon_matches("errors happened") is False
    assert error_lexicon_matches("PO export finished: <*> POs written to <*>") is False
    assert error_lexicon_matches("") is False
    assert error_lexicon_matches("x" * 10_000) is False


def test_severity_detect_prefers_configured_then_level_fields_then_status_then_lexicon() -> None:
    fields = {"lvl": "warn", "level": "error", "status": "503"}
    assert detect_severity(fields, "x", severity_field="lvl") == Severity.WARN
    assert "lvl" not in fields
    assert fields["level"] == "error"
    fields = {"level": "info", "http_status": "503"}
    assert detect_severity(fields, "payment request failed") == Severity.INFO
    assert "level" not in fields
    assert fields == {"http_status": "503"}
    fields = {"httpStatus": "503", "status": "ERROR"}
    assert detect_severity(fields, "paymentMessage") == Severity.ERROR
    assert fields == {"httpStatus": "503", "status": "ERROR"}
    fields = {"status": "404"}
    assert detect_severity(fields, "x") == Severity.WARN
    assert detect_severity({"status": "200"}, "x") is None
    assert detect_severity({}, "SFTP upload failed: Permission denied <*>") == Severity.ERROR
    assert detect_severity({}, "cart created") is None
    assert detect_severity({"log.level": "debug"}, "failed") == Severity.DEBUG
    assert detect_severity({"severity_number": "17"}, "x") == Severity.ERROR
    assert detect_severity({"severity_number": "9"}, "x") == Severity.INFO
    assert detect_severity({"severity_number": "1"}, "x") == Severity.TRACE
    assert detect_severity({"severity_number": "24"}, "x") == Severity.FATAL
    assert detect_severity({"severity_number": "25"}, "x") is None


def test_severity_detect_unknown_level_value_falls_through_and_is_kept() -> None:
    fields = {"level": "banana", "status": "503"}
    assert detect_severity(fields, "x") == Severity.ERROR
    assert fields["level"] == "banana"


def test_severity_detect_without_consuming() -> None:
    fields = {"level": "info"}
    assert detect_severity(fields, "x", consume=False) == Severity.INFO
    assert fields == {"level": "info"}


def test_severity_field_lists_are_documented() -> None:
    assert {"level", "severity", "log.level", "loglevel"} <= set(LEVEL_FIELDS)
    assert {"status", "http_status", "status_code", "httpStatus"} <= set(STATUS_FIELDS)


@settings(max_examples=200, deadline=1000)
@given(st.text(max_size=50), st.text(max_size=200))
def test_severity_property_never_raises(level: str, template: str) -> None:
    result = map_level(level)
    assert result is None or isinstance(result, Severity)
    severity_from_status(level)
    error_lexicon_matches(template)
    detect_severity({"level": level, "status": level}, template)
