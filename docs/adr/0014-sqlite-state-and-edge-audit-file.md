# ADR 0014: SQLite in WAL mode for edge state; a hash-chained audit file at the edge

Status: accepted, 2026-10-08 (spec 8.4 "Reveal vault", 8.5, 14.9, 2.3 invariant 6)

## Context

The edge keeps four kinds of durable state: the disk buffer of batches waiting for core, the
reveal vault (token to ciphertext), connector cursors, and the audit trail of reveals and
tokenize calls. Spec 8.5 allows "append-only segment files (or SQLite in WAL mode)". Spec 14.9
defines the audit log as a PostgreSQL table in core, which the edge does not reach.

## Decision

- `buffer.sqlite`, `vault.sqlite`, `cursors.sqlite` and `secrets.sqlite` (ADR 0013) are SQLite
  databases in WAL mode under the edge state directory, one writer each, opened with
  `check_same_thread=False` behind a lock. The buffer stores zstd-compressed batches with their
  byte size, so the 20 GB cap and the 80% backpressure threshold are exact.
- The edge audit trail is `audit.ndjson`: append-only, each row carrying `prev_hash` and
  `row_hash = sha256(prev_hash || canonical_json(row))` exactly as spec 14.9 defines for the
  core table, verifiable with `carto-edge audit verify`. The core API (M3) records every reveal
  and search in the central audit log as well; the edge file is the second, independent record
  that a compromised core cannot rewrite.

## Alternatives

- Segment files for the buffer: more code for the same durability; SQLite's WAL gives atomic
  appends and cheap size accounting.
- Forward audit rows to core over an internal endpoint: adds an endpoint and a dependency on
  core being up for a reveal to be recorded; the file is always writable.

## Consequences

Backups of the edge state directory (runbook) cover the vault, cursors and audit file. The
support bundle (M6) includes the audit file's verification result, never its contents.
