"""logfmt parsing (spec 8.2 item 3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.parse.logfmt import looks_like_logfmt, parse_logfmt

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)


def test_parse_logfmt_orders_line() -> None:
    line = (
        'ts=2026-09-23T13:16:46.566Z level=info msg="order created from cart" order_id=4471 '
        "cart_id=c-88213 total=129.99 channel=web"
    )
    assert parse_logfmt(line) == {
        "ts": "2026-09-23T13:16:46.566Z",
        "level": "info",
        "msg": "order created from cart",
        "order_id": "4471",
        "cart_id": "c-88213",
        "total": "129.99",
        "channel": "web",
    }


def test_parse_logfmt_quoting_and_escapes() -> None:
    line = r'a="x \"quoted\" y" b="back\\slash" c="eq=in" d="" e=plain f="tab\there"'
    assert parse_logfmt(line) == {
        "a": 'x "quoted" y',
        "b": "back\\slash",
        "c": "eq=in",
        "d": "",
        "e": "plain",
        "f": "tab\there",
    }


def test_parse_logfmt_bare_words_are_not_logfmt() -> None:
    """A bare word is usually an unquoted message running on; taken as a flag it would turn
    the words, names included, into field names (spec 2.3 invariant 2)."""
    assert parse_logfmt("  level=info   debug  retry=3 ") is None
    assert parse_logfmt("msg=refund approved for MKBOB Higgins order_id=4471") is None


def test_parse_logfmt_spacing() -> None:
    assert parse_logfmt("  level=info     retry=3 ") == {
        "level": "info",
        "retry": "3",
    }


def test_parse_logfmt_unterminated_quote_is_lenient() -> None:
    assert parse_logfmt('msg="open ended value') == {"msg": "open ended value"}


def test_parse_logfmt_duplicate_keys_last_wins() -> None:
    assert parse_logfmt("a=1 a=2") == {"a": "2"}


def test_parse_logfmt_rejects_non_logfmt() -> None:
    assert parse_logfmt("") is None
    assert parse_logfmt("PO export finished: 412 POs written") is None
    assert parse_logfmt("=value") is None
    assert parse_logfmt('{"a": 1}') is None
    access = '127.0.0.1 - - [23/Sep/2026:13:04:06 +0000] "GET / HTTP/1.1" 200 1'
    assert parse_logfmt(access) is None
    assert parse_logfmt('a=1 "stray" b=2') is None


def test_looks_like_logfmt_needs_a_leading_pair() -> None:
    assert looks_like_logfmt("ts=1 level=info")
    assert looks_like_logfmt("log.level=info")
    assert looks_like_logfmt("@ts=1")
    assert not looks_like_logfmt("hello a=1")
    assert not looks_like_logfmt("")
    assert not looks_like_logfmt('{"a":1}')


@needs_sim
def test_parse_logfmt_every_orders_and_shipping_line_from_the_simulator() -> None:
    for folder, pattern in (("orders", "*.log"), ("shipping", "shipping-app-*.log")):
        files = sorted((SIM_DIR / folder).glob(pattern))
        assert files, folder
        for line in files[0].read_text(encoding="utf-8").splitlines()[:500]:
            parsed = parse_logfmt(line)
            assert parsed is not None, line[:40]
            assert {"ts", "level", "msg"} <= parsed.keys()


@settings(max_examples=200, deadline=2000)
@given(st.text(max_size=200))
def test_parse_logfmt_property_never_raises(text: str) -> None:
    result = parse_logfmt(text)
    assert result is None or all(isinstance(v, str) for v in result.values())


@settings(max_examples=100, deadline=2000)
@given(
    st.dictionaries(
        st.from_regex(r"\A[a-z_][a-z0-9_.]{0,8}\Z"),
        st.text(alphabet=st.characters(blacklist_categories=("Cc", "Cs")), max_size=20),
        min_size=1,
        max_size=6,
    )
)
def test_parse_logfmt_property_round_trips_quoted_values(pairs: dict[str, str]) -> None:
    def render(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    line = " ".join(f"{k}={render(v)}" for k, v in pairs.items())
    assert parse_logfmt(line) == pairs
