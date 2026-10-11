# ADR 0027: Values never travel as field names, one-off templates or rare kept values

Status: accepted for M1, 2026-10-10 (spec 2.3 invariant 2, 8.2, 8.3, 18.3). One residual risk
is recorded below for the founder.

## Context

The M1 review of the offline analyzer found values reaching core through channels the
classifier never sees, because only field values are classified:

1. **Field names.** Names travel in clear (attribute keys, identifier and dropped field names,
   the `keys:` template of ADR 0016). A JSON map keyed by order id (`{"c-88213": {...}}`), a CSV
   data row taken for the header, or a logfmt line whose message runs on unquoted
   (`msg=refund approved for Bob Higgins`, where `approved`, `for`, `Bob` and `Higgins` became
   flags) put values in names.
2. **Templates.** Drain3 keeps a cluster with one member verbatim, so a message seen once (a
   delivery note, a code word, a name in an unusual sentence) became a template constant. Template
   text travels in clear on every event and in `templates.json`.
3. **Rare kept values.** Rule 6 keeps a low-cardinality field in clear when its samples pass the
   detector. The samples are a reservoir, so a value seen once or twice is likely unsampled: a
   name in a field that otherwise holds `yes` and `no` went out in clear.
4. **Upload metadata.** File events carried the analyst's full directory path.

The leak test missed these because the simulator never produced them, and the scanners matched
only string values, not keys or dotted paths.

## Decision

- **Names that look like values are masked.** Every path segment with an `@` after its first
  character, a run of three or more digits, or more than 64 characters becomes `*`, in field
  paths and in the `keys:` signature (`parse/common.py`).
- **CSV headers must look like names.** A first row that does not (empty, duplicate, digits,
  `@`) is data with positional names `col_0` and onwards.
- **Logfmt has no bare flags.** A bare word means the line is not logfmt; it falls back to text.
- **Templates need three members.** Until a Drain3 cluster has three members its template is all
  parameters (`<*> <*> <*>`), so every word of a one-off message is a parameter and is classified
  like any other value. The analyzer mines each run from scratch so two runs over the same input
  give the same templates; template ids are hashes of system and text either way (spec 8.2).
- **Kept values need three sightings.** Field statistics count each value exactly, in memory
  only, for the first 2,048 distinct values of a field. A `keep` field's value travels in clear
  only once that exact value was seen three times; otherwise it is dropped and listed in
  `dropped_fields`. A field past 2,048 distinct values is not an attribute, so its counts are freed
  and none of its values is kept again. The same gate filters the bundle's sample values. Counts
  are never persisted, so after a gateway restart a kept value needs three fresh sightings.
- **Upload file events carry the directory's own name**, not its path.
- **The leak test plants probes** for each class (map keys, free text in a field, a one-off
  message, a logfmt line with bare words, rare values in a kept field) and both scanners match
  keys and dotted paths. Carto tokens are removed before matching, since a four-digit order id
  can appear inside a token's base64 by chance. Actor logins and order amounts are now markers.

## Consequences

- A message type seen once or twice has an all-parameter template, so its first events group
  under a generic template. From the third member on, the real template appears.
- A kept attribute value seen fewer than three times is missing from its first events. In
  gateway mode this also applies to the first two events of each value after a restart.
- Field names with a three-digit run (`line_100`, `http_200_count`) are masked to `*`. Pins and
  dashboards that name such fields need the masked form.
- Memory: up to 2,048 short values per low-cardinality field; identifier fields free theirs once
  they pass the cap.

## Residual risk (founder question)

A JSON map keyed by a value with no digit run, such as a customer name (`{"Alice Smith": 3}`),
still puts that name in a field path. Closing it needs map detection: a parent path with many
distinct child names is a map, and its keys become values. That changes the field model (spec
8.2 flattening), so it is not done without a decision. Scenario A has no such map.

## Addendum, 2026-10-11: the analyzer's second pass

The analyzer reads its input twice (ADR 0017) and used to mine templates in both passes, so a
message seen twice reached three cluster members in pass 2 and travelled with its words as
constants (`parcel left with neighbour <name>` in clear in the events, `templates.json` and
`MANIFEST.md`). The template store now freezes before pass 2: it matches messages against the
clusters of pass 1 and never grows them. The gateway reads each record once and was not
affected. The leak test plants a message seen twice.
