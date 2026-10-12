# ADR 0042: Edge state decisions from the M1 review (work package D)

Status: proposed, 2026-10-12 (plan `docs/plans/M1-review-fixes.md`, package D; spec 2.3
invariants 6 and 7, 8.4, 8.5, 14.4, 14.9). Records the choices made while fixing the edge state,
key and audit findings that ADR 0033 does not already settle. The founder reviews it with the
package.

## 1. The audit anchor line is written beside the log processor, not through it

**Context.** ADR 0033 publishes the hourly audit anchor (`seq`, `row_hash`, `ts`) as a product
log line. Every product log line passes `carto_common.logging.redact` (spec 14.12), which shapes
any lower-case hex run that mixes letters and digits: a 64-hex `row_hash` would be logged as
`AA9999...`, and the anchor could no longer be compared with the chain. Package B also makes the
processor mask integers of four or more digits, which would hide `seq`.

**Decision.** `carto_edge.audit_anchor` renders the anchor line itself (the same JSON keys as
the structlog renderer: `event`, `service`, `level`, `timestamp`) and writes it to the gateway's
log stream. Before writing it checks that `seq` is a non-negative integer, `row_hash` matches
`[0-9a-f]{64}` and `ts` is an ISO 8601 timestamp; anything else raises and nothing is written.
No other line bypasses the processor.

**Why this does not weaken invariant 7.** The line can carry only a row number, a chain hash
and a time. The chain hash covers the whole row including its timestamp and the previous hash,
so it cannot be inverted or dictionary-matched to an identifier the way a hash of a bare value
could. An exemption inside the processor keyed on a field name was rejected: any caller could
then log a 64-hex digest of a raw identifier under that name.
