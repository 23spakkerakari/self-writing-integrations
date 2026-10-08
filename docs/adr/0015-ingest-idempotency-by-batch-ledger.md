# ADR 0015: Ingest idempotency through a batch ledger; ClickHouse tables stay plain MergeTree

Status: accepted, 2026-10-08 (spec 7.2, 8.1 "at-least-once delivery", 8.5 "Idempotent by event_id")

## Context

The edge delivers at least once; core must not double-count. Spec 7.2 defines `events` and
`event_identifiers` as `MergeTree` tables. `ReplacingMergeTree` would dedupe `events` (its
ordering key ends in `event_id`) but not `event_identifiers`, whose key `(tenant_id, token,
observed_at)` is shared by legitimately distinct rows, so engine-level deduplication cannot be
applied uniformly.

## Decision

`ingest-api` keeps `ingest_batches (tenant_id, batch_id, source_id, received_at, event_count)`
in PostgreSQL. For each `POST /internal/ingest`:

1. look the batch id up; if present, acknowledge with `duplicate: true` and write nothing;
2. insert the rows into ClickHouse;
3. insert the ledger row.

A write failure between steps 2 and 3 leaves no ledger row, so the edge's retry writes again;
the concurrent-delivery race can therefore write one batch twice. The linker and assembler count
distinct tokens and distinct event ids, so duplicates affect storage, not results; M2 adds
`FINAL`/`GROUP BY` where exact counts matter. The bundle loader derives chunk batch ids from the
bundle id, so re-loading a bundle is idempotent through the same ledger.

## Alternatives

- `ReplacingMergeTree` for both tables with `event_id` appended to the ordering key: changes the
  spec's DDL and the ordering key that trace search relies on.
- ClickHouse `insert_deduplication_token`: works per block for replicated tables and with
  `non_replicated_deduplication_window` otherwise; fragile across block splitting and client
  versions.

## Consequences

The ledger grows by one row per batch (about 17 rows per day per source at 5,000 events per
batch and 1,000 events per minute); it is pruned with the event retention period.
