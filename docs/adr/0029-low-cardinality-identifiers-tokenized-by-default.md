# ADR 0029: Values with a run of four or more digits are identifiers, whatever their field's cardinality

Status: accepted by the founder, 2026-10-11 (spec 8.3 rules 5 and 6, 9.7, 2.3 invariant 2).
Answers the open question of ADR 0026.

## Context

ADR 0026 closed the batch-key leak of scenario A (`manifest_id` `MAN-20260923-01`, nightly file
names `SHIP_20260923_2112.csv`) with admin pins. A field that nobody pins still goes out in
clear: spec 8.3 rule 6 keeps any field at or below the distinct-value threshold whose samples
pass the PII checks, and the statistics cannot tell a batch key from a warehouse code (`DC-03`).

## Decision

The founder chose a safe default. A **digit run** is four or more consecutive ASCII digits.

1. **Field rule.** Rule 6 gains a test before it keeps a field: when at least half of the
   field's samples contain a digit run, the field is an `identifier` (reason
   `identifier:digit_run`) and is tokenized with the identifier forms. Batch keys stay usable as
   join keys for the linker (spec 9.7), as tokens.
2. **Value rule.** In a field that is kept anyway (fewer than half of its samples have a digit
   run), a single value with a digit run does not travel in clear: it is left out of the event's
   attributes like a value seen fewer than three times (ADR 0027), and left out of the
   `MANIFEST.md` samples.
3. **Pins win.** A field pinned by an admin to `keep` (a year, a port, a build number) skips both
   rules; the reviewer has looked at it. Rule 1 (secrets) still comes before pins.

Short codes stay in clear: `DC-03`, `200`, `503`, `us-east`, `v12`. Rules 3 and 4 run first, so
dates, timestamps and amounts keep their own classes and forms.

## Alternatives

- Pins only (ADR 0026): an unpinned batch key leaks if the review misses it.
- Flag such fields in `MANIFEST.md` without changing the policy: same leak, more noise.
- A three-digit run: would tokenize HTTP status codes and most short codes the spec names as
  attributes to keep.

## Consequences

- Harmless low-cardinality values with four digits (years, ports, build numbers, four-digit
  store numbers) are tokenized or left out until a reviewer pins the field to `keep`. The
  install guides say so in the review step.
- The scenario A pins for `manifest_id` and `file` are now redundant; they stay, with their
  reasons, as examples of pins. The `name` pin is still needed (the person-name hint drops it).
- `make eval` deltas are reported in the PR.
