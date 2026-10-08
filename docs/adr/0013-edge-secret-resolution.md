# ADR 0013: Edge secret resolution: Vault KV and a KMS-wrapped local store; cloud managers later

Status: accepted, 2026-10-08 (spec 8.1 "Credentials are referenced, never stored", 14.3)

## Context

Spec 14.3 names the `secret_ref` schemes `vault://`, `aws-sm://`, `azure-kv://`, `gcp-sm://` and
`local://`, the last described as "KMS-wrapped and stored in PostgreSQL; Compose pilots only".
Connector credentials are resolved at the edge, and the edge has no PostgreSQL connection (it
talks only to sources and to `ingest-api`); giving it one would widen the trust boundary.

## Decision

`carto_edge.secrets.EdgeSecretResolver` resolves:

- `vault://<mount>/<path>[#<field>]` against Vault KV v2 (`GET /v1/<mount>/data/<path>`), with
  the Vault token read from the file named by `kms.vault_token_file`.
- `local://<name>` against a SQLite store under the edge state directory whose values are
  AES-256-GCM encrypted with a data key wrapped by the configured KMS (`keys/secrets-data-key.json`).
  `carto-edge secret set <name>` writes a value from a prompt or a file, never from an argument.
- `aws-sm://`, `azure-kv://`, `gcp-sm://` raise a clear error naming this ADR until the cloud
  SDK wrappers land (ADR 0012, M6 at the latest).

Values are cached in memory for at most 15 minutes, never written to disk unencrypted, never
returned by any API and never logged (spec 14.3).

## Alternatives

- Resolve secrets in core and push them to the edge: core would hold credentials, which the
  edge/core split exists to avoid.
- Environment variables: forbidden by spec 8.4 for keys and a poor fit for rotation.

## Consequences

Core's `local://` store (for channel credentials, M4) stays in PostgreSQL as the spec says; the
scheme name is shared, the backing store differs by side of the trust boundary.
