# ADR 0021: Double Metaphone through the `metaphone` package

Status: accepted, 2026-10-08 (spec 8.4 `phonetic.k`, 0.1 item 9)

## Context

Spec 8.4 defines `phonetic.k` as the Double Metaphone code of each name token. PyPI offers:
`metaphone` (BSD-3, pure Python, Double Metaphone, last released 2016), `jellyfish` (MIT,
maintained, original Metaphone only), `abydos` (GPL-3, unmaintained), and several abandoned
forks. The spec's dependency bar is "boring, actively maintained, permissive".

## Decision

Use `metaphone` (`from metaphone import doublemetaphone`). It is permissive and pure Python, and
the Double Metaphone algorithm has not changed since 2000, so the lack of releases is not a
maintenance risk; the package imports and runs under Python 3.12. The tokenizer uses the primary
code only. Should the package ever break, the alternative is to vendor its ~800 lines into
`carto_edge.pipeline.forms` under its BSD licence.

## Alternatives

- `jellyfish.metaphone`: maintained but a different, less precise algorithm than the spec names.
- Vendor now: more code to own for no present gain.

## Consequences

`metaphone` is listed in the M1 dependency table with its licence. Renovate will not find updates
for it; the vendoring option is recorded here.
