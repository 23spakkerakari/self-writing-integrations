# ADR 0022: Bundle signature: a per-run Ed25519 key proves integrity, not origin

Status: accepted, 2026-10-08 (spec 4.1, 8.1.1 "and a signature")

## Context

Spec 8.1.1 says the bundle contains "a signature" and that the tokenization key never leaves the
customer. The customer runs `carto-edge analyze` on their own machine with no PKI, no account
with us and no shared secret.

## Decision

`carto-edge analyze` generates an Ed25519 key for the run, signs the exact bytes of
`manifest.json` (which carries the sha256 and size of every data file), and writes
`signature.json` with the public key, its id, the manifest digest and the signature. The private
key is discarded. `carto-core verify-bundle` and `load-bundle` verify the signature, the manifest
digest and every file digest before reading an event, and refuse bundles with extra files.

The signature proves the bundle was not altered after `analyze` wrote it (by transport, storage
or a careless edit). It does not prove who produced it: origin is established by the customer's
secure hand-over, as it would be for any file they send. A future option can sign with a key the
customer registers with us.

## Alternatives

- HMAC with the tokenization key: ties bundle verification to a key that must never leave the
  customer, so we could never verify it.
- Detached signature of each file: more files, same guarantee.

## Consequences

`carto_schema.bundle.BundleSignature` is part of the exported contracts. The MANIFEST.md tells
the customer what the signature does and does not mean.
