# ADR 0023: One token per key version for a field and form in the canonical event

Status: accepted, 2026-10-08 (spec 7.1, 8.4 "Rotation"; amends the M0 canonical event rule)

## Context

Spec 8.4 says rotation dual-tokenizes new events with both key versions for an overlap window.
The M0 canonical event rejected two identifier entries with the same `(field, form)`, which made
dual tokenization unrepresentable.

## Decision

`CanonicalEvent.identifiers` entries are unique per `(field, form, key version)`, the key version
being the token's `t<n>.` prefix. During the overlap window an event carries, for each field and
form, one entry per live key version, active key first; the 64-entry cap (spec 7.1) is applied
with active-key entries first so a long overlap never displaces the current key's tokens. Core
groups identifiers by key version (spec 8.4: "Linker and assembler treat links per key version").

## Alternatives

- Suffix the field name with the key version: breaks `field_ref` stability across rotations.
- Separate `identifiers_previous` list: a schema change for a transient state.

## Consequences

The JSON Schema is regenerated; the uniqueness rule is model-only (JSON Schema cannot express
it), as the M0 shape rule already was.
