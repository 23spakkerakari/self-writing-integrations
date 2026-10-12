"""Regression tests for the M1 review findings on field names and structure (plan
M1-review-fixes, package A1; ADR 0030 amendment of 2026-10-12).

Every value is synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from carto_edge.config import ParseConfig, SourceConfig, SourceType
from carto_edge.pipeline.model import ParsedRecord, RawRecord
from carto_edge.pipeline.parse.common import clean_text, flatten
from carto_edge.pipeline.parse.json import parse_json
from carto_edge.pipeline.parse.names import FieldNameStore
from carto_edge.pipeline.parser import RecordParser
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.templates import TemplateStore
from carto_schema.event import EventKind

SYS = "sys_web"
RECEIVED = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
LONE = chr(0xD800)
FFFD = chr(0xFFFD)


def _source(parse: ParseConfig | None = None) -> SourceConfig:
    return SourceConfig(
        id="src_web",
        system=SYS,
        type=SourceType.UPLOAD,
        config={"paths": ["x.log"]},
        parse=parse or ParseConfig(),
    )


def _raw(text: str, line: int = 1, locator: str | None = None) -> RawRecord:
    return RawRecord(
        source_id="src_web",
        system_id=SYS,
        kind=EventKind.LOG,
        locator=locator or f"app.log:line:{line}",
        received_at=RECEIVED,
        text=text,
        sequence=line,
    )


# -- finding 7: lone surrogates are replaced at the decode boundary ------------------------------


def test_clean_text_replaces_lone_surrogates_only() -> None:
    assert clean_text("plain") == "plain"
    assert clean_text("café \U0001f600") == "café \U0001f600"
    assert clean_text(f"a{LONE}b{chr(0xDFFF)}c") == f"a{FFFD}b{FFFD}c"


def test_flatten_and_json_never_return_a_lone_surrogate() -> None:
    flat = parse_json('{"msg": "payment \\ud800 received", "k\\udc00": {"n\\ud801": 1}}')
    assert flat is not None
    text = json.dumps(flat.fields, ensure_ascii=False) + "".join(flat.top_keys)
    text.encode("utf-8")  # raises on a lone surrogate
    assert flat.fields["msg"] == f"payment {FFFD} received"
    nested = flatten({f"a{LONE}": [f"v{LONE}"]})
    "".join([*nested.fields, *nested.fields.values()]).encode("utf-8")


def test_a_text_record_with_a_lone_surrogate_parses() -> None:
    parser = RecordParser(_source(), TemplateStore(), FieldNameStore(RegexDetector()))
    result = parser.parse(_raw(f"2026-09-23T04:00:00Z INFO payment {LONE} received"))
    assert isinstance(result, ParsedRecord)
    assert parser.internal_errors == 0
    json.dumps(result.fields, ensure_ascii=False).encode("utf-8")


def test_the_name_register_persists_names_with_surrogates(tmp_path: Path) -> None:
    names = FieldNameStore(RegexDetector(), tmp_path, flush_interval_seconds=0)
    for _ in range(4):
        names.apply("sys_b", {f"k{LONE}": "1", "plain_key": "2"}, [f"k{LONE}", "plain_key"])
    names.close()
    again = FieldNameStore(RegexDetector(), tmp_path)
    gated = again.apply("sys_b", {"plain_key": "2"}, ["plain_key"])
    assert gated.fields == {"plain_key": "2"}  # the register was written and read back
