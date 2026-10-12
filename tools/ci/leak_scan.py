"""Leak scan for the Compose smoke run (spec 18.3): no simulator marker in core rows or logs.

The simulator plants unique marker values in every sensitive and high-cardinality field and
lists them in ``ground_truth/markers.json``. This script reads that file and scans NDJSON
exports of ClickHouse tables (``--rows``: every key and string value of every row) and plain
text (``--text``: the product logs, line by line).

The matching rule is the one of ``edge/tests/test_edge_leak.py``, so the two scanners agree:

- text is lower-cased, and carto tokens (``t1.`` plus 22 base64url characters) are removed
  first, since a 4-digit order id can appear inside one by chance and a token carries no value;
- a marker with a letter in it matches anywhere (inside names and dotted paths too);
- a marker made of digits and punctuation matches only as a whole number, not inside a longer
  run of letters or digits (a ULID or a timestamp);
- markers shorter than four characters, and markers that are their own shape (only ``9``,
  ``a`` and punctuation once lower-cased, like order id ``9999``), are skipped.

A hit is reported with its category, file and column; the data is synthetic. Exit status 1 when
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

MIN_MARKER_LEN = 4
TOKEN = re.compile(r"t[1-9][0-9]{0,3}\.[A-Za-z0-9_-]{22}")


def _trie_pattern(words: Iterable[str]) -> str:
    trie: dict[str, Any] = {}
    for word in words:
        node = trie
        for char in word:
            node = node.setdefault(char, {})
        node[""] = {}

    def build(node: dict[str, Any]) -> str:
        branches = [re.escape(char) + build(child) for char, child in sorted(node.items()) if char]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        return f"(?:{body})?" if "" in node else body

    return build(trie)


def _own_shape(text: str) -> bool:
    return all(char in "9a" or not char.isalnum() for char in text)


def leaves(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """``(where, text)`` for every key and string leaf of a JSON value."""
    if isinstance(value, str):
        yield path or "$", value
    elif isinstance(value, dict):
        for key, child in value.items():
            name = str(key)
            yield f"{path}{{key}}", name
            yield from leaves(child, f"{path}.{name}" if path else name)
    elif isinstance(value, list):
        for child in value:
            yield from leaves(child, f"{path}[]")


class Markers:
    def __init__(self, data: dict[str, Any]) -> None:
        self.category: dict[str, str] = {}
        for top, value in data.items():
            for where, text in leaves(value, top):
                if where.endswith("{key}"):
                    continue
                marker = text.strip().lower()
                if len(marker) >= MIN_MARKER_LEN and not _own_shape(marker):
                    self.category.setdefault(marker, where.split(".")[0].split("[")[0])
        with_letter = [m for m in self.category if any(c.isalpha() for c in m)]
        without = [m for m in self.category if not any(c.isalpha() for c in m)]
        self.anywhere = re.compile(_trie_pattern(with_letter)) if with_letter else None
        self.whole = (
            re.compile(rf"(?<![0-9a-z])(?:{_trie_pattern(without)})(?![0-9a-z])")
            if without
            else None
        )

    def __len__(self) -> int:
        return len(self.category)

    def find(self, text: str) -> Iterator[str]:
        lowered = TOKEN.sub("<token>", text.lower())
        for pattern in (self.anywhere, self.whole):
            if pattern is not None:
                for match in pattern.finditer(lowered):
                    yield self.category[match.group(0)]


def scan_rows(path: Path, markers: Markers) -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            for where, text in leaves(json.loads(line)):
                column = where.split(".")[0].split("{")[0].split("[")[0]
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
        if hits:
            for (category, where), count in sorted(hits.items()):
                print(f"LEAK {path.name}: {count} {category} marker(s) in {where}")
            total += sum(hits.values())
        else:
            print(f"clean {path.name} ({path.stat().st_size} bytes)")
    print(f"{len(markers)} markers checked; {total} hits")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
