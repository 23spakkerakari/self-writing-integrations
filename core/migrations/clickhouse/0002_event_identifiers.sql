-- event_identifiers: exploded identifier forms, the join workhorse (spec 7.2). One statement
-- per file. {events_days} is the configured retention (spec 14.10: identifiers follow events).
CREATE TABLE IF NOT EXISTS event_identifiers (
  tenant_id LowCardinality(String),
  token String,
  field_ref LowCardinality(String),   -- system_id/template_id/field
  form LowCardinality(String),
  shape LowCardinality(String),
  event_id String,
  system_id LowCardinality(String),
  observed_at DateTime64(3, 'UTC')
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(observed_at)
ORDER BY (tenant_id, token, observed_at)
TTL toDateTime(observed_at) + INTERVAL {events_days} DAY;
