# carto-schema

The edge-to-core contract of carto, defined once as Pydantic v2 models and exported as JSON
Schema (spec Section 7.1, 8.4, 8.5 and 12; M0 plan "Package contracts fixed in M0"). Every other
package imports these models instead of redefining field names or limits.

```python
from carto_schema import CanonicalEvent, IngestBatch, SourceHeartbeat, shape, token_domain

event = CanonicalEvent.example()          # the spec 7.1 example, tokens spelled out in full
event.model_dump_json()                   # compact JSON, key order as in the spec
CanonicalEvent.model_validate_json(text)  # strict: unknown keys and naive timestamps fail
```

## What is in the package

| Module | Contents |
| --- | --- |
| `carto_schema.event` | `CanonicalEvent`, `Identifier`, `Actor`, `Redaction`; enums `EventKind`, `ObservedAtQuality`, `ActorKind`, `Severity`; the limits; id patterns (`ULID_PATTERN`, `TENANT_ID_PATTERN`, `SOURCE_ID_PATTERN`, `TEMPLATE_ID_PATTERN`); the `UtcTimestamp` field type |
| `carto_schema.forms` | form names (`is_form`, `parse_form`, `FORM_PATTERN`), token domains (`token_domain`), the token format (`TOKEN_PATTERN`), value shapes (`shape`, `SHAPE_MAX_LEN`); the `Form`, `Token` and `Shape` field types |
| `carto_schema.ingest` | `IngestBatch` (`POST /internal/ingest`), `SourceHeartbeat` and `SourceStatus` (`POST /internal/heartbeat`) |
| `carto_schema.cli` | the `carto-schema` command: `export`, `check`, `example` |
| `schemas/` | the committed JSON Schema files (Draft 2020-12), one per model |

Every model uses `extra="forbid"`, `frozen=True` and `hide_input_in_errors=True`:

- a payload with a field the model does not know is rejected;
- attribute assignment on an instance is rejected; the `list` and `dict` fields stay ordinary
  containers, so a copy that was changed in place must be validated again before it is
  forwarded;
- `str()` and `repr()` of a `ValidationError` never echo the rejected value, which may be the
  clear text this contract exists to keep out of core's logs (spec 2.3 invariant 7).
  `ValidationError.errors()` and `.json()` still carry `input` unless called with
  `include_input=False`, which anything that logs or returns them (the M1 ingest handler) must
  do.

## The canonical event (spec 7.1)

Fields, in JSON order:

| Field | Rule |
| --- | --- |
| `schema_version` | the string `"1"` |
| `event_id` | ULID, 26 upper-case Crockford base32 characters, first character `0` to `7` |
| `tenant_id` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `source_id`, `system_id` | `^[a-z][a-z0-9_]{0,63}$` |
| `kind` | `log`, `row_change`, `file_arrived`, `file_removed`, `http_access`, `webhook` |
| `observed_at`, `ingested_at` | timestamps, see below |
| `observed_at_quality` | `source`, `ingest`, `inferred` |
| `template_id` | `^[A-Za-z0-9_.:-]{1,64}$` |
| `template_text` | at most 4,096 characters, constants only (see the note at the end) |
| `severity` | `trace`, `debug`, `info`, `warn`, `error`, `fatal` or `null` (default) |
| `attributes` | string to string, at most 256 entries, keys 1 to 128 characters, values at most 256 characters; values cross the boundary in clear, so only fields the edge classified `low_card_attribute` (non-sensitive, at or below the distinct-value threshold) may appear (spec 7.1, 8.3 rule 6) |
| `identifiers` | at most 64 entries; `(field, form)` pairs unique within the event |
| `actor` | `{token, kind}` with `kind` in `human`, `service`, `unknown`, or `null` (default) |
| `dropped_fields` | at most 1,024 field names of 1 to 256 characters, default empty |
| `redaction` | `{policy_version: 1..32 chars, entities_masked: int >= 0}` |

An identifier entry is `{field, form, token, shape, len}`: `field` is the source field name
(1 to 256 characters), `form` one of the form names below, `token` a token, `shape` and `len`
describe the form value the token was computed from. `shape` is 1 to 64 characters and must be
what `shape()` returns (`shape(value) == value`), so a raw value in the shape slot is rejected;
`len` is a strict integer of at least 1 (no booleans, no numeric strings).

**Timestamps.** Only ISO 8601 text with an offset (or a `datetime` object in Python) is
accepted; numbers such as epoch seconds or milliseconds are rejected, convert them at the edge.
Input must be timezone-aware; a naive value is a validation error. The model stores the value
converted to UTC and truncated to milliseconds. JSON output is always
`YYYY-MM-DDTHH:MM:SS.mmmZ` (for example `2026-10-06T21:12:03.412Z`); `model_dump()` in Python
mode keeps `datetime` objects.

## Tokens and forms (spec 8.4)

A token is `t<key_version>.<22 base64url characters>`, for example
`t1.q8Jm0h3cR2VfZp4Lx9sT1w`: the key version is 1 to 4 digits without a leading zero, the body
is the first 22 characters of `base64url(HMAC-SHA256(K_tenant, domain || 0x00 || form_value))`
with no padding. This package validates the format only; computing tokens is the edge's job.

Form names and their HMAC domains:

| Form | Domain | Meaning |
| --- | --- | --- |
| `raw` | `id` | exact string after NFKC and trim |
| `norm` | `id` | `raw` lowercased |
| `alnum` | `id` | `norm` without non-alphanumerics |
| `digits.0` to `digits.2` | `id` | k-th digit run of 4 or more digits, leading zeros stripped (at most 3 runs) |
| `date` | `date` | ISO 8601 date |
| `amount` | `amt` | integer minor units |
| `phonetic.0` to `phonetic.7` | `ph` | Double Metaphone code of the k-th name token |

`raw`, `norm`, `alnum` and `digits.<k>` share the `id` domain so that `4471` from a raw field
matches `4471` extracted from `SO-0004471`.

**Shape** (spec 8.3): `shape(value)` maps digits to `9` and letters to `A`, keeps punctuation and
whitespace, collapses a run of more than 12 identical shape characters to 12 plus `+`, and cuts
the result at 64 characters. `SO-0004471` has shape `AA-9999999`; twenty digits give
`999999999999+`.

## Ingest batch and heartbeat (spec 8.5, 12)

`IngestBatch`: `schema_version`, `tenant_id`, `source_id`, `batch_id` (ULID), `sent_at`,
`events` (1 to 5,000 canonical events, every one with the batch's `tenant_id` and `source_id`).
The 5 MB cap of spec 8.5 is enforced by the transport, not by the model.

`SourceHeartbeat`: `schema_version`, `tenant_id`, `source_id`, `sent_at`, `status` (`ok`,
`degraded`, `failing`, `paused`), `last_success_at` (nullable), `lag_seconds` (finite, >= 0),
`error_count` and `buffer_depth` (>= 0), `oldest_buffered_at` (nullable), `message` (at most
1,024 characters, default empty). The numeric fields are strict, as in the exported schema:
booleans and numeric strings are rejected, `lag_seconds` accepts an integer. `message` is
operator-facing free text that core stores as sent; the edge applies its log redaction to it
before sending (no secrets, connection strings or raw identifier values, spec 2.3 invariant 7).

## JSON Schema files

`schemas/` holds `canonical_event.v1.schema.json`, `ingest_batch.v1.schema.json` and
`source_heartbeat.v1.schema.json`, generated from the models in validation mode with
`$schema` set to Draft 2020-12, `$id` set to `urn:carto:schema:<name>:v1`, sorted keys,
two-space indent, UTF-8 and LF line endings. Three rules cannot be expressed in JSON Schema and
are enforced by the Pydantic models only: the uniqueness of `(field, form)` pairs, the tenant
and source agreement inside a batch, and `shape(value) == value` for `Identifier.shape`.

- Regenerate after changing a model: `make schema`
  (`uv run carto-schema export --out packages/carto-schema/schemas`).
- Check for drift, as CI does: `make schema-check`
  (`uv run carto-schema check --dir packages/carto-schema/schemas`; exit code 1 lists the files
  that differ or are missing).
- Print the example event: `uv run carto-schema example`.
- `main()` returns exit codes and never calls `sys.exit`: usage errors return 2 and `--help`
  returns 0, so it can be embedded in-process. Paths are printed with backslash escapes when
  stdout's encoding cannot represent them (a Windows pipe on a legacy code page).

## Note on `template_text`

`template_text` must carry template constants only: never raw message text, never a value from
an identifier or free-text field (spec 2.3 invariant 2, spec 8.3 rule 7). The edge enforces
this when it mines templates and classifies fields. This package can only bound the length; it
cannot tell a constant from a leaked value, so a test of the edge pipeline is where that
guarantee lives.
