"""CSV records per source, with a header row or configured columns (spec 8.2 item 4).

A CSV line only means something together with its header, so :class:`CsvParser` is stateful per
source: with ``csv_columns`` configured it names the cells after them (and still consumes a
first header row when ``csv_has_header`` is on); without them it learns the columns from the
first row it sees, and :meth:`CsvParser.reset` forgets them when the next file starts (the
schema rename of scenario A arrives as a new file with a new header). Without a header and
without columns the cells are ``col_0..n``. Cells are split with the standard ``csv`` module on
the configured single-character delimiter; empty cells are absent fields (an exported NULL);
cells beyond the known columns become ``_extra_<index>``. A record that spans lines (a quoted
newline) is beyond a line-oriented reader; connectors that read whole CSV files hand rows over
as ``fields`` instead. The auto-detector only treats a line as a header when it is
conservatively header-shaped (:func:`looks_like_csv_header`), so free text with a comma in it
does not turn a text source into CSV.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from carto_edge.config import ParseConfig
from carto_edge.pipeline.parse.common import MAX_FIELDS

__all__ = ["CsvHeader", "CsvParser", "looks_like_csv_header", "parse_csv_row", "split_csv_line"]

MAX_COLUMNS: Final = MAX_FIELDS
_HEADER_CELL: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")
_FIELD_SIZE_LIMIT: Final = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CsvHeader:
    """Returned when a line was consumed as the header row, not as a record."""

    columns: tuple[str, ...]


def split_csv_line(text: str, delimiter: str) -> list[str] | None:
    """Split one line into cells; ``None`` for a blank line or an unsplittable one."""
    line = text.rstrip("\r\n")
    if not line.strip():
        return None
    try:
        rows = list(csv.reader([line], delimiter=delimiter, strict=False))
    except (csv.Error, ValueError, TypeError):
        return None
    if not rows:
        return None
    return rows[0][:MAX_COLUMNS]


def parse_csv_row(text: str, columns: Sequence[str], delimiter: str = ",") -> dict[str, str] | None:
    """Map the cells of one line onto ``columns``; empty cells are absent."""
    cells = split_csv_line(text, delimiter)
    if cells is None:
        return None
    fields: dict[str, str] = {}
    for index, cell in enumerate(cells):
        if cell == "":
            continue
        name = columns[index] if index < len(columns) else f"_extra_{index}"
        if not name:
            name = f"col_{index}"
        fields[name] = cell
    return fields


def looks_like_csv_header(text: str, delimiter: str) -> bool:
    """A conservative header shape: two or more distinct, non-empty, name-like cells.

    Name-like means a letter or underscore followed by letters, digits, ``_``, ``.`` or ``-``
    (no spaces, so ``export scheduler idle, next run 21:10`` is not a header), and no cell may
    be numeric. Sources whose headers do not fit configure ``format: csv``.
    """
    cells = split_csv_line(text, delimiter)
    if cells is None or len(cells) < 2:
        return False
    if any(not _HEADER_CELL.match(cell) for cell in cells):
        return False
    return len(set(cells)) == len(cells)


class CsvParser:
    """Per-source CSV state: the known columns and the delimiter (spec 8.2 item 4)."""

    def __init__(self, config: ParseConfig) -> None:
        self._delimiter = config.csv_delimiter
        self._configured = tuple(config.csv_columns) if config.csv_columns else None
        self._has_header = config.csv_has_header
        self._columns: tuple[str, ...] | None = self._configured
        self._header_pending = self._has_header

    @property
    def columns(self) -> tuple[str, ...] | None:
        """The columns in use: configured, learned from a header, or ``None`` before either."""
        return self._columns

    @property
    def ready(self) -> bool:
        """True once rows can be parsed without consuming a header first."""
        return self._columns is not None and not self._header_pending

    def reset(self) -> None:
        """Forget a learned header (a new file starts); configured columns stay."""
        self._columns = self._configured
        self._header_pending = self._has_header

    def parse(self, text: str, *, strict: bool = False) -> dict[str, str] | CsvHeader | None:
        """Parse one line: a header marker, the row's fields, or ``None`` for a blank line.

        With ``strict`` (the auto-detector's mode) a row must have exactly as many cells as
        there are columns, so a free-text line with a comma in it is not taken for a row.
        """
        if self._header_pending:
            cells = split_csv_line(text, self._delimiter)
            if cells is None:
                return None
            header = tuple(cell.strip() for cell in cells)
            self._header_pending = False
            if self._columns is None:
                self._columns = header
            return CsvHeader(columns=header)
        if self._columns is None:
            cells = split_csv_line(text, self._delimiter)
            if cells is None:
                return None
            return parse_csv_row(text, [f"col_{i}" for i in range(len(cells))], self._delimiter)
        if strict:
            cells = split_csv_line(text, self._delimiter)
            if cells is None or len(cells) != len(self._columns):
                return None
        return parse_csv_row(text, self._columns, self._delimiter)
