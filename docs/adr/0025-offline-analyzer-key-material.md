# ADR 0025: The offline analyzer creates a local-KMS tenant key under its state directory

Status: accepted, 2026-10-09 (spec 4.1, 8.1.1, 8.4; ADR 0012, 0017)

## Context

Spec 8.4 says the tenant key is "generated at install" and unwrapped into edge-gateway memory at
startup; `carto-ctl key init` does that for an installed edge. The offline analyzer (spec 4.1) runs
before any install, on an analyst's machine, yet it must tokenize with a real key: the bundle's
tokens have to be consistent within the bundle and across re-runs (the customer reviews, fixes a
policy pin, runs again, and core loads the second bundle idempotently), and spec 8.1.1 says the
bundle never contains the key or the reveal vault.

## Decision

`carto-edge analyze` builds its runtime with `init_local_keys=True`
(`carto_edge.runtime.ensure_local_keys`): when `<state-dir>/keys/rotation.json` does not exist and
the KMS provider is `local` (the default), it creates the local KMS master key and version 1 of
the tenant key exactly as `carto-ctl key init --kms local` would, in the same file layout, then
loads them. When they exist they are reused, so the same state directory yields the same tokens
and, because the template store persists there too, the same template ids. With any other
provider the analyzer refuses to start and names `carto-ctl key init`. The reveal vault of the run
is written under the same state directory and stays there. The default state directory is
`<out>.edge-state` next to the bundle.

## Alternatives

- An ephemeral key per run: tokens would differ between runs of the same input, so a corrected
  bundle could not replace an earlier one and reveal would be impossible later.
- Require `carto-ctl key init` first: one more tool and one more step in the pilot journey that
  spec 4.1 wants to keep to "download, run, review, send".

## Consequences

The state directory is as sensitive as an installed edge's: the install guide tells the analyst
to keep it on an encrypted disk, back it up if later reveal is wanted, and never send it with the
bundle. A later installed edge that should keep the pilot's tokens can start from that state
directory (same key, same templates).
