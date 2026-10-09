"""CSV parsing: header row or configured columns, delimiter (spec 8.2 item 4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.config import ParseConfig, RecordFormat
from carto_edge.pipeline.parse.csv import (
    CsvHeader,
    CsvParser,
    looks_like_csv_header,
    parse_csv_row,
    split_csv_line,
)

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)

WAREHOUSE_HEADER = (
    "id,po_num,order_ref,status,warehouse_code,order_total,order_date,customer_name,"
    "ship_to_address,created_by,created_at,updated_at"
)
WAREHOUSE_ROW = (
    '1,88-210,SO-0004471,SHIPPED,DC-01,129.99,2026-09-23,Jane Smith,"282 mk5349da48 Street, '
    'Omaha, NE 52733",svc_wms_integration,2026-09-23 09:21:04,2026-09-23 21:41:31'
)


def test_parse_csv_row_with_warehouse_header_and_quoted_cell() -> None:
    columns = WAREHOUSE_HEADER.split(",")
    row = parse_csv_row(WAREHOUSE_ROW, columns)
    assert row == {
        "id": "1",
        "po_num": "88-210",
        "order_ref": "SO-0004471",
        "status": "SHIPPED",
        "warehouse_code": "DC-01",
        "order_total": "129.99",
        "order_date": "2026-09-23",
        "customer_name": "Jane Smith",
        "ship_to_address": "282 mk5349da48 Street, Omaha, NE 52733",
        "created_by": "svc_wms_integration",
        "created_at": "2026-09-23 09:21:04",
        "updated_at": "2026-09-23 21:41:31",
    }


def test_parse_csv_row_empty_cells_are_absent_and_extras_are_named() -> None:
    assert parse_csv_row("1,,3", ["a", "b", "c"]) == {"a": "1", "c": "3"}
    assert parse_csv_row("1,2", ["a", "b", "c"]) == {"a": "1", "b": "2"}
    assert parse_csv_row("1,2,3,4", ["a", "b"]) == {
        "a": "1",
        "b": "2",
        "_extra_2": "3",
        "_extra_3": "4",
    }


def test_parse_csv_row_custom_delimiter_and_quotes() -> None:
    assert parse_csv_row('a;"b;c";d', ["x", "y", "z"], delimiter=";") == {
        "x": "a",
        "y": "b;c",
        "z": "d",
    }
    assert parse_csv_row("a\tb", ["x", "y"], delimiter="\t") == {"x": "a", "y": "b"}


def test_parse_csv_row_rejects_empty_and_blank() -> None:
    assert parse_csv_row("", ["a"]) is None
    assert parse_csv_row("   ", ["a"]) is None


def test_split_csv_line_handles_quotes_and_crlf() -> None:
    assert split_csv_line('a,"b,c",d\r\n', ",") == ["a", "b,c", "d"]
    assert split_csv_line('a,"unterminated', ",") == ["a", "unterminated"]
    assert split_csv_line("", ",") is None


def test_looks_like_csv_header_is_conservative() -> None:
    assert looks_like_csv_header(WAREHOUSE_HEADER, ",")
    assert looks_like_csv_header(
        "po_num,order_ref,warehouse_code,carrier,service_level,weight_kg", ","
    )
    assert not looks_like_csv_header("export scheduler idle, next run 21:10", ",")
    assert not looks_like_csv_header("1,2,3", ",")
    assert not looks_like_csv_header("single", ",")
    assert not looks_like_csv_header("a,a", ",")
    assert not looks_like_csv_header("a,,b", ",")
    assert not looks_like_csv_header("ts=1,level=info", ",")
    assert not looks_like_csv_header("", ",")


def test_csv_parser_learns_the_header_then_parses_rows() -> None:
    parser = CsvParser(ParseConfig(format=RecordFormat.CSV))
    first = parser.parse(WAREHOUSE_HEADER)
    assert isinstance(first, CsvHeader)
    assert first.columns[:3] == ("id", "po_num", "order_ref")
    assert parser.columns == first.columns
    row = parser.parse(WAREHOUSE_ROW)
    assert isinstance(row, dict)
    assert row["po_num"] == "88-210"


def test_csv_parser_renamed_header_after_reset() -> None:
    parser = CsvParser(ParseConfig(format=RecordFormat.CSV))
    parser.parse(WAREHOUSE_HEADER)
    parser.reset()
    renamed = parser.parse(WAREHOUSE_HEADER.replace("po_num", "po_number"))
    assert isinstance(renamed, CsvHeader)
    row = parser.parse(WAREHOUSE_ROW)
    assert isinstance(row, dict)
    assert "po_number" in row
    assert "po_num" not in row


def test_csv_parser_configured_columns_skip_the_header_learning() -> None:
    parser = CsvParser(
        ParseConfig(format=RecordFormat.CSV, csv_columns=["a", "b"], csv_has_header=False)
    )
    assert parser.parse("1,2") == {"a": "1", "b": "2"}
    assert parser.parse("3,4") == {"a": "3", "b": "4"}


def test_csv_parser_configured_columns_with_header_row_consumes_it() -> None:
    parser = CsvParser(ParseConfig(format=RecordFormat.CSV, csv_columns=["a", "b"]))
    assert isinstance(parser.parse("x,y"), CsvHeader)
    assert parser.parse("1,2") == {"a": "1", "b": "2"}


def test_csv_parser_without_header_or_columns_numbers_the_columns() -> None:
    parser = CsvParser(ParseConfig(format=RecordFormat.CSV, csv_has_header=False))
    assert parser.parse("1,2,3") == {"col_0": "1", "col_1": "2", "col_2": "3"}


def test_csv_parser_delimiter_from_config() -> None:
    parser = CsvParser(ParseConfig(format=RecordFormat.CSV, csv_delimiter=";"))
    assert isinstance(parser.parse("a;b"), CsvHeader)
    assert parser.parse("1;2") == {"a": "1", "b": "2"}


def test_csv_parser_rejects_blank_lines() -> None:
    parser = CsvParser(
        ParseConfig(format=RecordFormat.CSV, csv_columns=["a"], csv_has_header=False)
    )
    assert parser.parse("") is None
    assert parser.parse("   ") is None


@needs_sim
def test_csv_parser_warehouse_export_and_ship_files_from_the_simulator() -> None:
    parser = CsvParser(ParseConfig(format=RecordFormat.CSV))
    lines = (SIM_DIR / "warehouse" / "purchase_orders.csv").read_text(encoding="utf-8").splitlines()
    assert isinstance(parser.parse(lines[0]), CsvHeader)
    for line in lines[1:200]:
        row = parser.parse(line)
        assert isinstance(row, dict), line[:40]
        assert set(row) >= {"id", "po_num", "order_ref", "status", "created_at"}
    renamed = SIM_DIR / "warehouse" / "purchase_orders.renamed.csv"
    if renamed.exists():
        parser.reset()
        renamed_lines = renamed.read_text(encoding="utf-8").splitlines()
        header = parser.parse(renamed_lines[0])
        assert isinstance(header, CsvHeader)
        assert "po_number" in header.columns
    ship_files = sorted((SIM_DIR / "shipping" / "outbound").glob("SHIP_*.csv"))
    assert ship_files
    parser.reset()
    ship_lines = ship_files[0].read_text(encoding="utf-8").splitlines()
    assert isinstance(parser.parse(ship_lines[0]), CsvHeader)
    row = parser.parse(ship_lines[1])
    assert isinstance(row, dict)
    assert set(row) == {
        "po_num",
        "order_ref",
        "warehouse_code",
        "carrier",
        "service_level",
        "weight_kg",
    }


@settings(max_examples=200, deadline=2000)
@given(st.text(max_size=200), st.sampled_from([",", ";", "\t", "|"]))
def test_parse_csv_property_never_raises(text: str, delimiter: str) -> None:
    result = parse_csv_row(text, ["a", "b", "c"], delimiter=delimiter)
    assert result is None or all(isinstance(v, str) for v in result.values())
    looks_like_csv_header(text, delimiter)
