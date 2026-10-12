# ADR 0012: KMS providers in M1 are the local file KMS and Vault Transit; cloud KMS wrappers follow

Status: accepted, 2026-10-08 (spec 8.4 "Key management", 14.3, 14.4)

## Context

Spec 8.4 requires every key to be stored only wrapped by the customer's KMS or Vault Transit, with
a local KMS fallback for Compose pilots. Supporting AWS KMS, Azure Key Vault and GCP KMS means
three cloud SDKs (boto3, azure-identity plus azure-keyvault-keys, google-cloud-kms), each with
its own credential chain, in the edge image whose whole point is to be small and auditable.

## Decision

`carto_common.crypto` defines a `KeyWrapper` protocol (`wrap`, `unwrap`, `provider`, `key_id`)
and a `WrappedKey` file format that binds a context (tenant, purpose, version) into the
wrapping. M1 ships two implementations:

- `LocalKms`: a 32-byte master key in a file with owner-only permissions (`carto-ctl key init
  --kms local`), AES-256-GCM wrapping with the canonical context as AAD. For development and
  Compose pilots only; the runbook says how to back the file up and that losing it loses the
  vault and the joinability of old tokens.
- `VaultTransitKms`: HashiCorp Vault Transit `encrypt`/`decrypt` with the context as the
  derived-key context, token read from a file.

Cloud KMS wrappers are added behind the same protocol when a design partner's environment needs
one, and no later than M6 (production hardening). Each will be a separate optional dependency
group so the edge image only carries the SDK it uses.

## Alternatives

- Ship all three now: dependency weight and credential-chain surface for code no partner can
  exercise yet.
- Only the local KMS: fails spec 8.4 for any production install.

## Consequences

`EdgeSettings.kms.provider` is `local` or `vault` in M1. The `secret_ref` schemes for cloud
secret managers are also deferred (ADR 0013). The install guide states which providers exist.
