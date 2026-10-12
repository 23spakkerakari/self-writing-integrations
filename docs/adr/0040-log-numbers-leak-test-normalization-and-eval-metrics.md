# ADR 0040: Numbers in product logs, a normalizing leak test, and eval metric definitions

Status: accepted, 2026-10-12 (spec 2.3 invariants 2 and 7, 14.12, 18.1, 18.3, 18.4; review fixes
work package B, docs/plans/M1-review-fixes.md). Amends ADR 0010 rule 7.

## Context

The M1 review found that the product-log processor passed every `int` and `float` through
untouched (ADR 0010 rule 7), so `log.info("x", customer_id=48812213, total=207.81)` wrote both
values in clear, and the tests locked that behaviour in. The rule relied on callers logging
identifiers as strings, which nothing enforced.

## Decision

### Numbers in product logs are values unless their key names a metric

- An `int` or `float` (never a `bool`) whose decimal text holds four or more digits, leading zeros
  not counted, is logged as its shape (`48812213` -> `"99999999"`, `207.81` -> `"999.99"`), the
  same shape rule as identifier-shaped strings. Four digits matches the string identifier rule
  (ADR 0010 rule 1); `0.0123` has three digits, so short durations and ratios stay readable.
- The number is logged as is when the last segment of the key it sits directly under (split
  like secret-named keys) is one of `METRIC_KEY_QUALIFIERS`: `count`, `counts`, `attempt`,
  `attempts`, `retries`, `rows`, `records`, `events`, `batches`, `samples`, `bytes`, `ms`,
  `seconds`, `second`, `days`, `depth`, `size`, `len`, `length`, `limit`, `port`, `status`,
  `pid`, `version`, `versions`. The list covers what the product logs today (`events=`,
  `duration_ms=`, `retry_in_seconds=`, `events_per_second=`, `policy_version=`) and stays
  short on purpose: `total` and `num` are not in it, because `total=207.81` is an order amount
  and `order_num=4471` an identifier. A count is logged as `<thing>_count`.
- The qualifier applies to the number directly under the key, never to the items of a list
  under it: `events=[48812213]` is a list of values.
- Counts the product logs under other keys (`accepted`, `rejected`, `identifiers`,
  `samples_seen`) are masked once they reach four digits. That loses an exact count, never a
  value, and the call sites can be renamed to a `_count` key when an operator needs them.

## Consequences

- Operators keep counts, durations, sizes, ports and versions; a customer number, card number
  or amount logged as a number is masked like the same value logged as a string.
- The spec 18.3 leak test now scans JSON numbers in log lines, so a number
  logged under a metric key that happens to equal a marker still fails the build.
