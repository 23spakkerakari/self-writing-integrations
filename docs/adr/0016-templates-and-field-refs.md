# ADR 0016: Template derivation per record kind, template ids and field refs

Status: accepted, 2026-10-08 (spec 3 "Template", "Field", 5.4 step 3, 7.1, 7.2 `field_ref`, 8.2 rule 6)

## Context

Spec 8.2 says unstructured text goes through Drain3 and "each distinct template gets a stable
`template_id` (hash of system + template text)". It does not say what the template of a
structured record, a row, a file arrival or a webhook is, and `field_ref` (spec 7.2) is
"system_id/template_id/field", so every event needs a template.

## Decision

| Record kind | `template_text` |
| --- | --- |
| log with a message field (`msg`, `message`, `log`, `body`, `text`, or the configured field) | the Drain3 template of the message; parameters become fields `msg.param_0..n` |
| log without a message field (pure key-value) | `keys:<sorted top-level keys, at most 32>` |
| XML document | the root element name |
| unstructured text | the Drain3 template of the line after the leading timestamp and level; parameters `param_0..n` |
| access log | `<METHOD> <route with digit runs and UUIDs as *> <status class>xx` |
| row change | `row_change <query or table name>` (supplied by the connector as `template_hint`) |
| file arrived / removed | `file_arrived <file name with digit runs as *>` (`SHIP_*_*.csv`) |
| webhook | the value of the configured event-type field, else `keys:<...>` |

`template_id = "tpl_" + sha256(system_id + "\0" + template_text).hexdigest()[:12]`; the spec's
example `tpl_4f1c9a` is six hex characters, twelve keeps collisions negligible across a 50-system
install. `field_ref = system_id/template_id/path` where `path` is the flattened field path
(`payload.order.id`, `@attr`, `msg.param_1`, a column name). Drain3 state is persisted per system
so templates and therefore ids survive restarts (spec 8.2).

## Alternatives

- One template per source for structured records: loses the "steps inside one system"
  distinction (spec 9.2 item 2) that message templates provide.
- Hash of the key set for every structured record: ignores the message, which is where the
  event type lives in application logs.

## Consequences

Field profiles (M2) are keyed by `field_ref`; renaming a template in the UI (spec 9.8 "group
templates into one named event type") is a display mapping, never a change of the id.
