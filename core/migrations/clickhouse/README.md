# core/migrations/clickhouse

Ordered SQL migrations for ClickHouse with a migration table (spec Section 7.2). The `events`
and `event_identifiers` tables land in M1 with ingest-api; `txn_events`, `hop_stats_hourly` and
`field_value_samples` follow with the workers that write them.
