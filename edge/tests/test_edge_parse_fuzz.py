"""Property tests over the whole parse stage (spec 18.1: parsers never crash or hang on fuzzed
input; 2.3 invariant 8)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from carto_edge.config import ParseConfig, RecordFormat, SourceConfig, SourceType
from carto_edge.pipeline.model import ParsedRecord, RawRecord
from carto_edge.pipeline.parse.common import MAX_DEPTH, MAX_FIELDS, flatten
from carto_edge.pipeline.parser import ParseFailure, RecordParser
from carto_edge.pipeline.templates import TemplateStore
from carto_schema.event import MAX_TEMPLATE_TEXT_LEN, TEMPLATE_ID_PATTERN, EventKind

RECEIVED = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
FORMATS = list(RecordFormat)
STORE = TemplateStore()
PARSERS = {
    fmt: RecordParser(
        SourceConfig(
            id="src_fuzz",
            system="sys_fuzz",
            type=SourceType.UPLOAD,
            parse=ParseConfig(format=fmt, max_record_bytes=4096, timezone="America/New_York"),
        ),
        STORE,
    )
    for fmt in FORMATS
}


def _raw(text: str | None, fields: dict[str, Any] | None = None) -> RawRecord:
    return RawRecord(
        source_id="src_fuzz",
        system_id="sys_fuzz",
        kind=EventKind.LOG,
        locator="fuzz:line:1",
        received_at=RECEIVED,
        text=text,
        fields=fields,
    )


def _check(result: ParsedRecord | ParseFailure) -> None:
    assert all(parser.internal_errors == 0 for parser in PARSERS.values())
    if isinstance(result, ParseFailure):
        assert result.reason
        return
    assert result.observed_at.tzinfo is UTC
    assert TEMPLATE_ID_PATTERN.match(result.template_id)
    assert len(result.template_text) <= MAX_TEMPLATE_TEXT_LEN
    assert len(result.fields) <= MAX_FIELDS + 1024
    for key, value in result.fields.items():
        assert key
        assert isinstance(value, str)


@settings(max_examples=300, deadline=3000, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(max_size=600), st.sampled_from(FORMATS))
def test_fuzz_text_never_raises_in_any_format(text: str, fmt: RecordFormat) -> None:
    start = time.perf_counter()
    _check(PARSERS[fmt].parse(_raw(text)))
    assert time.perf_counter() - start < 2.0


@settings(max_examples=200, deadline=3000, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=600), st.sampled_from(FORMATS))
def test_fuzz_bytes_decoded_leniently_never_raise(data: bytes, fmt: RecordFormat) -> None:
    text = data.decode("utf-8", errors="replace")
    _check(PARSERS[fmt].parse(_raw(text)))


@settings(max_examples=100, deadline=3000)
@given(
    st.text(alphabet='{}[]":,\\ abc0123456789.-eEtrufalsn', max_size=400),
    st.text(alphabet="<>/=\"' ab?!&;#", max_size=300),
    st.text(alphabet='abc=" \\', max_size=200),
)
def test_fuzz_structured_looking_soup(json_soup: str, xml_soup: str, logfmt_soup: str) -> None:
    _check(PARSERS[RecordFormat.AUTO].parse(_raw(json_soup)))
    _check(PARSERS[RecordFormat.AUTO].parse(_raw(xml_soup)))
    _check(PARSERS[RecordFormat.AUTO].parse(_raw(logfmt_soup)))


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=15),
    lambda children: (
        st.lists(children, max_size=6) | st.dictionaries(st.text(max_size=6), children, max_size=6)
    ),
    max_leaves=60,
)


@settings(max_examples=150, deadline=3000)
@given(st.dictionaries(st.text(max_size=10), json_values, max_size=10))
def test_fuzz_fields_records_never_raise_and_respect_limits(fields: dict[str, Any]) -> None:
    result = PARSERS[RecordFormat.AUTO].parse(_raw(None, fields))
    _check(result)
    flat = flatten(fields)
    assert len(flat.fields) <= MAX_FIELDS
    for path in flat.fields:
        assert path
        assert path.count(".") <= MAX_DEPTH


@settings(max_examples=50, deadline=5000, suppress_health_check=[HealthCheck.too_slow])
@given(st.integers(min_value=1, max_value=200), st.sampled_from(["{", "[", "<a>", "(", '"']))
def test_fuzz_repetitive_nesting_is_bounded(count: int, piece: str) -> None:
    start = time.perf_counter()
    _check(PARSERS[RecordFormat.AUTO].parse(_raw(piece * count)))
    assert time.perf_counter() - start < 2.0


def test_fuzz_oversized_record_is_rejected_before_any_parser_runs() -> None:
    huge = "{" * 100_000
    result = PARSERS[RecordFormat.AUTO].parse(_raw(huge))
    assert result == ParseFailure("too_large")
    assert PARSERS[RecordFormat.XML].parse(_raw("<a>" * 50_000)) == ParseFailure("too_large")
