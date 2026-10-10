# ADR 0026: Low-cardinality identifiers (batch keys) are pinned in M1; a default safeguard is a founder question

Status: accepted for M1, 2026-10-10 (spec 8.3 rules 5 and 6, 9.7, 2.3 invariant 2, 18.3).
Open question for the founder recorded below.

## Context

Spec 8.3 rule 5 tokenizes a field whose distinct estimate is above the threshold (1,000, or 20%
of its count); rule 6 keeps a field at or below it in clear when its samples pass the PII checks.
Some identifiers have low cardinality by nature: a carrier manifest id or a nightly file name is
shared by every shipment of one night (spec 9.7 calls them batch keys). In scenario A,
`manifest_id` (`MAN-20260923-01`) and the shipping log's `file` (`SHIP_20260923_2112.csv`) take
one value per night, and the file-drop events carry the file name in a field called `name`. Rule
6 keeps the first two in clear, and the M1 leak test (spec 18.3) found `manifest_id` in the
bundle, the forwarded batches and the ClickHouse rows. (`name` is dropped by the person-name hint,
which hides the file from the linker for the opposite reason.)

The statistics cannot tell `MAN-20260923-01` from a warehouse code such as `DC-03`, which spec 8.3
lists as an attribute to keep. Spec 2.3 invariant 2 says identifiers are tokenized; it also says
"any value from a field with more than the configured distinct-value threshold", so a
low-cardinality identifier falls between the two sentences.

## Decision

For M1 the spec's own mechanism closes the gap: admin pins (spec 8.3, "Admins can pin a field's
class and policy"). `simulator/analyze.shop.yaml` and `deploy/compose/sources.dev.yaml` pin
`sys_shipping/*/manifest_id`, `sys_shipping/*/file` and `sys_shipping/*/name` as identifiers to
tokenize, with reasons. The classifier is unchanged and follows spec 8.3 exactly. The install
guides tell the reviewer to look for identifiers in the "kept in clear" table of `MANIFEST.md`
and pin them before the bundle leaves (spec 4.1 step 2 is that review).

## Open question for the founder

Should the classifier tokenize low-cardinality values that look like identifiers by default,
for example values with a digit run of four or more characters (`MAN-20260923-01`, file names
with dates) while short codes (`DC-03`, `200`, `us-east`) stay in clear? It would make the edge
safer by default and the pins optional, at the cost of tokenizing some harmless values (ports,
years, build numbers) that a reviewer would then pin to keep. It changes spec 8.3 rule 6, so it
is not done without a decision.

## Consequences

A customer whose batch keys are not pinned sends them in clear until a reviewer pins them; the
offline analyzer's `MANIFEST.md` lists every kept field with sample values so the review can
catch them before anything leaves. The leak test passes on scenario A with the pins.
