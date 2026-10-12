# ADR 0017: The offline analyzer classifies in two passes; the gateway streams with quarantine

Status: accepted, 2026-10-08 (spec 4.1, 8.1.1, 8.3 rule 8, 2.3 invariant 3)

## Context

Spec 8.3 classifies fields from streaming statistics and quarantines a field until 200 samples
exist (tokenize if identifier-shaped, else drop). In a long-running gateway that is the right
behaviour: the first events of a new field are protected and later ones are classified. In the
offline analyzer the whole input is available up front, and a bundle whose first 200 events of
each field were handled differently from the rest would confuse the customer's review ("why is
`status` tokenized in some events?") and the linker.

## Decision

`carto-edge analyze` runs two passes over its inputs:

1. **Statistics pass:** parse every record, mine templates, feed field statistics and PII
   sampling. Nothing is emitted.
2. **Emit pass:** parse again, classify with the complete statistics (no quarantine, because
   every field has all the samples it will ever have), tokenize, write events, fields and
   templates.

Parsing twice costs under a minute per gigabyte and keeps memory flat (no buffering of parsed
records). The gateway keeps the spec's streaming rule and logs every re-classification.

## Alternatives

- Buffer pass-1 records in memory: unbounded memory for the 20 GB uploads spec 8.1.1 allows.
- Single pass with quarantine in the analyzer: inconsistent bundles, as described above.

## Consequences

Field decisions in a bundle are final for that bundle; `MANIFEST.md` can list kept fields with
sample values honestly. A field with fewer than 200 samples in the whole input is still
classified by the rules, with `samples_seen` recorded, so the reviewer sees the thin evidence.
