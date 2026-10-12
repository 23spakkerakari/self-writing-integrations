"""Record parsers in spec 8.2 order: JSON/NDJSON, XML, logfmt, CSV, access logs, unstructured
text. Every parser is bounded (size, depth, time) and never raises on malformed input; it
returns ``None`` so the caller can count the drop (spec 2.3 invariant 8)."""
