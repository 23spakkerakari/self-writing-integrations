# ADR 0043: Core decisions from the M1 review (work package E)

Status: proposed, 2026-10-15 (plan `docs/plans/M1-review-fixes.md`, package E; spec 2.3
invariants 2 and 8, 5.5, 8.5, 14.4, 14.6). Records the choices made while fixing the core
findings. It does not repeat ADR 0034 or ADR 0036; most of what they decide is not built yet
(see Consequences). The founder reviews it with the package.

## 1. Ingest decodes at most one bounded zstd frame

**Context.** An 18-byte frame that declares a 128 MiB window made the decompressor allocate
129 MiB per request before writing any output. Core read only the first frame, where the edge
reads across frames.

**Decision.** In `core/carto_core/ingest/app.py`:

- The decompressor refuses windows above `ZSTD_MAX_WINDOW_BYTES` (8 MiB). The edge compresses at
  level 3, with 2 MiB windows.
- A body must be exactly one frame. `_zstd_frame_size` walks the block headers (RFC 8878) to
  find the frame's end; a skippable frame, a second frame or trailing bytes are a 400, a
  malformed frame is a 400, and output above `ingest.max_decompressed_bytes` is a 413.
- `max_decompressed_bytes` defaults to 16 MiB (was 64 MiB), at most 128 MiB (was 512 MiB), and
  still at least `max_body_bytes` (5 MiB). A full batch of 5,000 events is a few MiB of JSON.

The edge parks a batch answered 400, 413 or 422.

## 2. Decoding runs off the event loop, and batches in flight are bounded

**Context.** Decompressing and validating a 5 MB batch on the event loop stalled every other
request for about a second.

**Decision.**

- The handler reads the capped body on the loop, then decompresses and validates it in
  Starlette's thread pool (`run_in_threadpool`).
- `ingest.max_inflight_batches` (default 8, 1 to 64) bounds the requests being read, decoded or
  written. It is a `threading.BoundedSemaphore`, only ever tried without waiting, so it is
  correct whatever loop or thread holds it. A request over the bound is answered 429 with
  `Retry-After: 1` and counted in `carto_ingest_busy_total`; the edge waits as told, capped at
  its own retry maximum.
- `/internal/ingest` holds its slot across read, decode, tenant check and write.
  `/internal/heartbeat` holds it for read and decode only; its single-row upsert runs outside.
- uvicorn's `limit_concurrency=256` is unchanged.

## 3. One ClickHouse client without a session

**Context.** ingest-api shares one clickhouse-connect client across its thread pool. With the
default generated session id, concurrent inserts ran in one session and failed with
`SESSION_IS_LOCKED`.

**Decision.** `create_client` passes `autogenerate_session_id=False`; nothing core does needs a
session, so one shared client stays enough and no client per thread or pool is needed.

## 4. Migrations run under a PostgreSQL advisory lock

**Context.** ClickHouse has no migration lock, so two migrate jobs (a Compose restart, two pods)
could apply the same file twice.

**Decision.** `migration_lock(engine)` (`core/carto_core/migrations.py`) takes the session-level
advisory lock `MIGRATION_LOCK_KEY` (`0x636172746F6D6967`, ASCII "cartomig") on its own
autocommit connection, so no transaction stays open. It tries first, logs that another run holds
it, then waits. It releases explicitly; when the release fails the connection is invalidated so
a pooled connection cannot keep the lock. `carto-core migrate` runs the ClickHouse migrations,
the retention TTLs and Alembic inside the lock, also with `--clickhouse-only`, which therefore
needs PostgreSQL too.

## 5. An older heartbeat never overwrites a newer one

**Decision.** `source_health` gains `sent_at` (Alembic `0002`), set to the earlier of the
heartbeat's `sent_at` and core's arrival time, so an edge whose clock runs ahead cannot pin the
row. The upsert updates only when the stored `sent_at` is NULL or not after the new one; on a tie
the later commit wins. A row written before `0002` is read by its `received_at`.

**Alternatives.** Ordering on `received_at` alone: arrival order is what goes wrong.

## 6. The canonical schema refuses names that carry values

**Context.** ADR 0027 and ADR 0030 mask value-shaped names on the edge. Core had no check of its
own, so an edge bug or an older edge could still deliver a value as a name.

**Decision.** In `packages/carto-schema/carto_schema/event.py`:

- Every dot-separated segment of a field name (`Identifier.field`, `dropped_fields`) is at most
  64 characters (`MAX_NAME_SEGMENT_LEN`), has no `@` after its first character (`@attr` stays
  valid) and no run of three digits (`NAME_DIGIT_RUN`). `*` and indexes below 20 pass. These are
  the edge's own masking rules.
- Attribute keys get the same checks, and their 1 to 128 length, in a validator that runs before
  field validation, because pydantic puts a dict key in the error location and would echo it.
  No error message names the field.
- `template_text` refuses a run of six digits (`MAX_TEMPLATE_DIGIT_RUN`); shorter numbers are
  protocol constants such as `HTTP/1.1 200`.
- `SchemaVersion` stays `"1"`: the rules are Python validators, and the committed JSON Schemas
  change only in their descriptions.

## 7. PostgreSQL connections verify the server by default

**Decision.** `PostgresSettings.sslmode` defaults to `verify-full` (`ca_file` becomes
`sslrootcert`); `prefer` and `allow` are not accepted. Turning verification off is explicit: the
Compose stack sets `CARTO_POSTGRES__SSLMODE: disable` on its internal network, and the
container tests do the same.

**Alternatives.** `require`: it encrypts but accepts any certificate, so it does not stop a
man in the middle.

## 8. Every Compose service has memory, CPU and process limits

**Decision.** `deploy/compose/compose.yaml` sets `mem_limit`, `cpus` and `pids_limit` on every
service, sized for the pilot VM of spec 5.5. The limits are not reservations, so the CPU caps may
add up to more than the VM has.

| Service | Memory | CPUs | Processes | Why |
| --- | --- | --- | --- | --- |
| clickhouse | 4g | 2.0 | 2048 | largest budget; reads its cgroup limit; many threads |
| edge-gateway | 2g | 2.0 | 256 | |
| postgres | 1g | 1.0 | 256 | |
| ingest-api | 1g | 2.0 | 256 | |
| simulator | 1g | 1.0 | 128 | dev profile only |
| migrate | 512m | 1.0 | 128 | one-shot |
| otel-collector | 512m | 1.0 | 256 | |
| pki-init, key-init | 256m | 0.5 | 64 | one-shot |
| proxy-otlp, proxy-webhooks | 128m | 0.5 | 64 | nginx, one worker (ADR 0032) |
| volumes-init | 64m | 0.25 | 32 | one-shot |

The two proxies came from `main` after the package was written; they were added when the package
was rebased on 2026-10-14. `core/tests/test_core_compose_limits.py` requires the three limits on
every service and the largest memory for ClickHouse, so a new service without limits fails CI.

## 9. Smaller decisions

- `load-bundle` closes the ClickHouse client and disposes the engine, also when the load fails
  (`cli._open_stores` is a context manager).

## Consequences

- **Not built, though decided by the founder:** ADR 0034 (the ledger still checks, writes, then
  records; `events` and `event_identifiers` are still `MergeTree`; no ledger pruning) and
  ADR 0036 (no `observed_at` range check, no split of inserts by partition day, and permanent
  ClickHouse errors still answer 503, so a batch spanning more than 100 partitions loops on the
  edge).
- **Not built, from the plan:** `load-bundle` keys chunks by index, not by event range, so a
  rerun with another `--chunk-size` duplicates events; it reads the files again after hashing
  them; it checks each event's tenant against the manifest but not the manifest against core's
  tenant; and its decompressor has no window cap.
- Spec 14.4 asks for TLS to PostgreSQL and ClickHouse. The Compose stack turns it off on its
  internal network (`sslmode: disable`, ClickHouse over HTTP), with only a YAML comment deferring
  TLS to M6. This ADR records that deferral; the founder confirms it.
- `RETRY_AFTER_SECONDS` is documented for 429 and 503, but only the 429 sends `Retry-After`.

## Tests

`core/tests/test_core_ingest_app.py`, `test_core_settings.py`, `test_core_clickhouse_rows.py`,
`test_core_cli.py`, `test_core_migrations_sql.py`, `test_core_compose_limits.py`,
`test_core_integration.py` (Docker only); `packages/carto-schema/tests/test_schema_event.py`,
`test_schema_property.py`.
