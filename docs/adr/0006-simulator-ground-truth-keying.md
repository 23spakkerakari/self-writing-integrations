# ADR 0006: Ground truth is keyed by source locator, and the eval harness joins through an edge locator map

Status: accepted, 2026-10-06

## Context

The eval harness (Section 18.4) must join what the engine produced (events with an `event_id`
assigned by the edge, transactions, links) to what the simulator knows is true. Native-format
simulator files must not carry a hidden transaction identifier: anything in the data is something
the engine could accidentally correlate on. `event_id` is assigned at the edge, deterministically
from source position (Section 8.1), but its exact derivation depends on timestamp parsing, which
is itself something the eval should test rather than assume.

## Decision

Every record the simulator emits gets a **locator key** `<source_id>:<locator>` where the
locator is:

- `<file name>:line:<n>` for a line in a log file (1-based, counted within that file),
- `<table>:row:<primary key>` for a database row,
- `file:<file name>` for a file arrival.

`ground_truth/event_txn.ndjson` maps each key to the true transaction id (or `null` for noise),
the true node, the true observed time in UTC, and the batch it belongs to, if any. Native files
carry nothing beyond what the system would really log.

In M1 the edge gains an eval-only option that writes a `locator_map.ndjson` (locator key to
`event_id`) next to its output. The harness joins engine output to truth through that map. No
locator information is sent to core.

## Alternatives

- Put a `sim_txn` field in every record: trivially correlatable, would invalidate the eval.
- Recompute `event_id` from the locator in the harness: couples the harness to the edge's
  timestamp parsing and key derivation; a parsing bug would silently misalign truth and output.

## Consequences

The simulator's writers must count lines per file and expose the primary keys they generate.
The M1 edge must implement the locator map option; until then the harness scores the empty
prediction and the ground truth itself.
