"""Record rendering: NDJSON, logfmt, XML lines, text, CSV, SQL (spec 8.2 parser formats)."""

from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from carto_simulator import clock, formats

LOGFMT_PAIR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')


def unquote(value: str) -> str:
    if value.startswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def test_timestamps() -> None:
    instant = datetime(2026, 9, 23, 13, 4, 6, 1000, tzinfo=UTC)
    assert formats.iso_utc_ms(instant) == "2026-09-23T13:04:06.001Z"
    local = clock.to_local(datetime(2026, 9, 23, 13, 4, 7, 120000, tzinfo=UTC))
    assert formats.iso_local_offset_ms(local) == "2026-09-23T09:04:07.120-04:00"
    assert formats.naive_local_seconds(local) == "2026-09-23 09:04:07"
    winter = clock.to_local(datetime(2026, 12, 23, 13, 4, 7, tzinfo=UTC))
    assert formats.iso_local_offset_ms(winter) == "2026-12-23T08:04:07.000-05:00"


def test_logfmt_quoting_and_escapes() -> None:
    line = formats.logfmt_line(
        [
            ("ts", "2026-09-23T13:04:06.001Z"),
            ("msg", "order created from cart"),
            ("order_id", 4471),
            ("note", 'say "hi" \\ bye'),
            ("eq", "a=b"),
            ("empty", ""),
            ("plain", "c-88213"),
        ]
    )
    assert line.startswith(
        'ts=2026-09-23T13:04:06.001Z msg="order created from cart" order_id=4471 '
    )
    pairs = {key: unquote(value) for key, value in LOGFMT_PAIR.findall(line)}
    assert pairs == {
        "ts": "2026-09-23T13:04:06.001Z",
        "msg": "order created from cart",
        "order_id": "4471",
        "note": 'say "hi" \\ bye',
        "eq": "a=b",
        "empty": "",
        "plain": "c-88213",
    }
    assert 'empty=""' in line and 'eq="a=b"' in line


def test_xml_line_escapes_and_parses() -> None:
    line = formats.xml_line("paymentMessage", [("cardholderName", "A & B <C>"), ("status", "OK")])
    assert "\n" not in line
    assert "&amp;" in line and "&lt;C&gt;" in line
    root = ET.fromstring(line)  # noqa: S314  (our own synthetic output)
    assert root.findtext("cardholderName") == "A & B <C>"
    assert root.findtext("status") == "OK"


def test_ndjson_and_text_lines() -> None:
    line = formats.ndjson_line({"ts": "2026-09-23T13:04:06.001Z", "total": 129.99, "items": 3})
    assert json.loads(line) == {"ts": "2026-09-23T13:04:06.001Z", "total": 129.99, "items": 3}
    assert line == '{"ts": "2026-09-23T13:04:06.001Z", "total": 129.99, "items": 3}'
    assert formats.text_line("2026-09-23 21:12:03", "INFO", "PO export finished") == (
        "2026-09-23 21:12:03 INFO PO export finished"
    )


def test_csv_text_quotes_commas() -> None:
    text = formats.csv_text(("a", "b"), [("1", "x, y"), ("2", 'say "hi"')])
    assert text == 'a,b\n1,"x, y"\n2,"say ""hi"""\n'


def test_sql_literals_execute_in_sqlite() -> None:
    assert formats.sql_string("O'Brien") == "'O''Brien'"
    statement = formats.sql_insert("t", ("id", "name", "total"), ("1", "'O''Brien'", "12.50"))
    assert statement == "INSERT INTO t (id, name, total) VALUES (1, 'O''Brien', 12.50);"
    connection = sqlite3.connect(":memory:")
    connection.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, total DECIMAL);")
    connection.executescript(statement)
    assert connection.execute("SELECT name, total FROM t").fetchone() == ("O'Brien", 12.5)
