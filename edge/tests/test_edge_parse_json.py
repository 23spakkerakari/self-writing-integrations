"""JSON/NDJSON parsing and the shared flattener (spec 8.2 item 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.parse.common import (
    MAX_ARRAY_ITEMS,
    MAX_DEPTH,
    MAX_FIELDS,
    NOTE_ARRAY_LIMIT,
    NOTE_DEPTH_LIMIT,
    NOTE_FIELD_LIMIT,
    flatten,
    key_signature,
    stringify,
    utf8_len,
)
from carto_edge.pipeline.parse.json import looks_like_json, parse_json

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)


def test_parse_json_webstore_line_flattens_scalars() -> None:
    line = (
        '{"ts": "2026-09-23T13:04:06.001Z", "level": "info", "msg": "cart created", '
        '"cart_id": "c-88213", "items": 3, "region": "us-east", "channel": "web"}'
    )
    parsed = parse_json(line)
    assert parsed is not None
    assert parsed.fields == {
        "ts": "2026-09-23T13:04:06.001Z",
        "level": "info",
        "msg": "cart created",
        "cart_id": "c-88213",
        "items": "3",
        "region": "us-east",
        "channel": "web",
    }
    assert parsed.top_keys == ("ts", "level", "msg", "cart_id", "items", "region", "channel")
    assert parsed.notes == ()


def test_parse_json_nested_paths_arrays_bools_floats_and_nulls() -> None:
    line = json.dumps(
        {
            "payload": {"order": {"id": 4471, "ok": True, "none": None}},
            "items": [{"sku": "A"}, {"sku": "B"}],
            "tags": ["x", "y"],
            "total": 129.99,
            "flag": False,
            "empty": {},
            "empty_list": [],
        }
    )
    parsed = parse_json(line)
    assert parsed is not None
    assert parsed.fields == {
        "payload.order.id": "4471",
        "payload.order.ok": "true",
        "items.0.sku": "A",
        "items.1.sku": "B",
        "tags.0": "x",
        "tags.1": "y",
        "total": "129.99",
        "flag": "false",
    }


def test_parse_json_rejects_non_objects_and_garbage() -> None:
    assert parse_json("[1, 2, 3]") is None
    assert parse_json('"text"') is None
    assert parse_json("42") is None
    assert parse_json("{not json") is None
    assert parse_json("") is None
    assert parse_json("   ") is None
    assert parse_json('{"a": 1} trailing') is None


def test_parse_json_rejects_nan_and_infinity() -> None:
    assert parse_json('{"a": NaN}') is None
    assert parse_json('{"a": Infinity}') is None
    assert parse_json('{"a": -Infinity}') is None


def test_parse_json_depth_limit_is_applied_and_noted() -> None:
    deep: dict[str, object] = {"leaf": 1}
    for level in range(MAX_DEPTH + 3):
        deep = {f"l{level}": deep}
    parsed = parse_json(json.dumps(deep))
    assert parsed is not None
    assert NOTE_DEPTH_LIMIT in parsed.notes
    assert all(path.count(".") < MAX_DEPTH for path in parsed.fields)


def test_parse_json_survives_pathological_nesting() -> None:
    assert parse_json("{" * 100_000 + "}" * 100_000) is None
    parsed = parse_json('{"a":' * 5000 + "1" + "}" * 5000)
    assert parsed is None or NOTE_DEPTH_LIMIT in parsed.notes


def test_parse_json_array_limit_is_applied_and_noted() -> None:
    parsed = parse_json(json.dumps({"items": list(range(MAX_ARRAY_ITEMS + 5))}))
    assert parsed is not None
    assert len(parsed.fields) == MAX_ARRAY_ITEMS
    assert f"items.{MAX_ARRAY_ITEMS - 1}" in parsed.fields
    assert f"items.{MAX_ARRAY_ITEMS}" not in parsed.fields
    assert NOTE_ARRAY_LIMIT in parsed.notes


def test_parse_json_field_limit_is_applied_and_noted() -> None:
    parsed = parse_json(json.dumps({f"k{i}": i for i in range(MAX_FIELDS + 10)}))
    assert parsed is not None
    assert len(parsed.fields) == MAX_FIELDS
    assert NOTE_FIELD_LIMIT in parsed.notes


def test_looks_like_json_is_a_cheap_prefix_check() -> None:
    assert looks_like_json('{"a": 1}')
    assert looks_like_json('  \t{"a": 1}')
    assert not looks_like_json("[1]")
    assert not looks_like_json("ts=1 level=info")
    assert not looks_like_json("")


def test_stringify_rules() -> None:
    assert stringify("x") == "x"
    assert stringify(True) == "true"
    assert stringify(False) == "false"
    assert stringify(7) == "7"
    assert stringify(1.5) == "1.5"
    assert stringify(None) is None
    assert stringify(b"\x00\xff") == "00ff"


def test_flatten_keeps_mapping_key_order_and_skips_empty_containers() -> None:
    flat = flatten({"b": {"c": [None, {}, []]}, "a": "1"})
    assert flat.fields == {"a": "1"}
    assert flat.top_keys == ("b", "a")


def test_flatten_handles_non_string_keys() -> None:
    flat = flatten({1: "one", ("t",): "tuple"})
    assert flat.fields == {"1": "one", "('t',)": "tuple"}


def test_key_signature_sorts_caps_and_sanitizes() -> None:
    assert key_signature(["msg", "ts", "level"]) == "keys:level,msg,ts"
    assert key_signature(["a b", "c,d", "e\nf"]) == "keys:a_b,c_d,e_f"
    many = key_signature([f"k{i:03d}" for i in range(50)])
    assert many.startswith("keys:k000,k001")
    assert many.count(",") == 31
    assert key_signature([]) == "keys:"


def test_utf8_len_counts_bytes() -> None:
    assert utf8_len("abc") == 3
    assert utf8_len("é") == 2
    assert utf8_len("\U0001f600") == 4


@needs_sim
def test_parse_json_every_webstore_line_from_the_simulator() -> None:
    files = sorted((SIM_DIR / "webstore").glob("*.ndjson"))
    assert files
    lines = files[0].read_text(encoding="utf-8").splitlines()[:500]
    for line in lines:
        parsed = parse_json(line)
        assert parsed is not None, line[:40]
        assert "ts" in parsed.fields
        assert "msg" in parsed.fields


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=40,
)


@settings(max_examples=150, deadline=2000)
@given(st.dictionaries(st.text(max_size=8), json_values, max_size=8))
def test_flatten_property_bounds_and_types(value: dict[str, object]) -> None:
    flat = flatten(value)
    assert len(flat.fields) <= MAX_FIELDS
    for path, text in flat.fields.items():
        assert isinstance(path, str)
        assert isinstance(text, str)
        assert path.count(".") < MAX_DEPTH + 1
    parsed = parse_json(json.dumps(value))
    assert parsed is not None
    assert parsed.fields == flat.fields


@settings(max_examples=200, deadline=2000)
@given(st.text(max_size=200))
def test_parse_json_property_never_raises(text: str) -> None:
    result = parse_json(text)
    assert result is None or isinstance(result.fields, dict)
