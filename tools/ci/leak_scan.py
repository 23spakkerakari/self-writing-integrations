"""Leak scan for the Compose smoke run (spec 18.3): no simulator marker in core rows or logs.

The simulator plants unique marker values in every sensitive and high-cardinality field and
lists them in ``ground_truth/markers.json`` (``identifier_values``, ``marker_tokens``,
``pii_values``). This script reads that file and scans:

- NDJSON exports of ClickHouse tables (``--rows``): only the string values of each row are
  scanned, so numbers such as lengths and timestamps cannot match a numeric marker by accident;
- plain text (``--text``): the product logs of every service, scanned line by line.

Matching is by whole token, case-insensitive: every string is split into tokens of letters,
digits, ``-``, ``_`` and ``.`` (the characters identifiers are made of), and a token that equals
a marker, or a marker value made of several tokens appearing as a contiguous run, is a hit.
Markers shorter than four characters are ignored, as in ``edge/tests/test_edge_leak.py``. The
data is synthetic, so a hit is reported with its category, file and column; exit status 1 when
anything was found, 0 otherwise, 2 on a usage error. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
MIN_MARKER_LEN = 4


def tokens(text: str) -> list[str]:
    return [match.group(0).lower().strip(".-_") for match in TOKEN.finditer(text)]


def leaves(value: Any) -> Iterator[str]:
    """Every string leaf of a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaves(item)


class Markers:
    """Marker values by category, matched as single tokens or contiguous token runs."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.single: dict[str, str] = {}
        self.multi: dict[tuple[str, ...], str] = {}
        for category, value in data.items():
            for text in leaves(value):
                if len(text.strip()) < MIN_MARKER_LEN:
                    continue
                parts = tuple(tokens(text))
                if len(parts) == 1:
                    self.single.setdefault(parts[0], category)
                elif parts:
                    self.multi.setdefault(parts, category)
        self.first_tokens = {parts[0] for parts in self.multi}

    def __len__(self) -> int:
        return len(self.single) + len(self.multi)

    def find(self, text: str) -> Iterator[str]:
        """The categories of the markers present in ``text``."""
        found = tokens(text)
        for index, token in enumerate(found):
            category = self.single.get(token)
            if category is not None:
                yield category
            if token in self.first_tokens:
                for parts, multi_category in self.multi.items():
                    if parts[0] == token and tuple(found[index : index + len(parts)]) == parts:
                        yield multi_category


def scan_rows(path: Path, markers: Markers) -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            for column, value in row.items():
                for text in leaves(value):
                    for category in markers.find(text):
                        hits[category, column] += 1
    return hits


def scan_text(path: Path, markers: Markers) -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            for category in markers.find(line):
                hits[category, "line"] += 1
    return hits


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--markers", type=Path, required=True, help="ground_truth/markers.json")
    parser.add_argument("--rows", type=Path, action="append", default=[], help="NDJSON export")
    parser.add_argument("--text", type=Path, action="append", default=[], help="log file")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.rows and not args.text:
        print("leak_scan: give at least one --rows or --text file", file=sys.stderr)
        return 2
    markers = Markers(json.loads(args.markers.read_text(encoding="utf-8")))
    if not len(markers):
        print("leak_scan: the markers file holds no marker", file=sys.stderr)
        return 2
    total = 0
    for path, scan in [(p, scan_rows) for p in args.rows] + [(p, scan_text) for p in args.text]:
        hits = scan(path, markers)
        size = path.stat().st_size
        if hits:
            for (category, where), count in sorted(hits.items()):
                print(f"LEAK {path.name}: {count} {category} marker(s) in {where}")
            total += sum(hits.values())
        else:
            print(f"clean {path.name} ({size} bytes)")
    print(f"{len(markers)} markers checked; {total} hits")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
