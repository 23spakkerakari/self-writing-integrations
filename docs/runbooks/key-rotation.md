# Runbook: tokenization key rotation (spec 8.4)

Tokens are HMACs under the tenant key `K_tenant`. Rotation introduces a new key version, keeps
the old one for an overlap window so new events carry tokens under both versions, then retires
it. Nothing stored in core is rewritten: the linker and assembler treat links per key version.

## When

- On schedule (recommended yearly), or
- immediately when the edge host, its state directory backup or the KMS key may have been
  exposed.

## Before you start

1. Confirm the KMS is reachable: `carto-edge key status` prints the active version, the KMS key
   id and the fingerprints (never the material).
2. Back up `<state_dir>/keys/` (wrapped keys only; useless without the KMS) and
   `<state_dir>/vault.sqlite`.
3. Decide the overlap window. Default 30 days; it must be at least the event retention period
   (`CARTO_RETENTION__EVENTS_DAYS`, default 30) so every stored event can still be joined with
   new ones.

## Rotate

```
carto-edge key rotate --overlap-days 30
```

This generates version `n+1`, wraps it with the KMS, writes `keys/tenant-key.json` (new active),
`keys/tenant-key.v<n>.json` (previous) and `keys/rotation.json`, and restarts tokenization with
both versions. The gateway reloads keys on the next batch; the offline analyzer reads them at
start. Every new event now carries one token per form per live version (ADR 0023); the reveal
vault stores raw values under both.

## During the overlap

- Trace search tokenizes queries under every live version, so searches match old and new events.
- Expect event size to grow by the identifier share for the window; the ClickHouse budget in
  spec 17 allows it.

## After the overlap

`carto-edge key status` shows the previous version with its `retire_at`. After that instant the
edge stops tokenizing with it automatically; delete `keys/tenant-key.v<n>.json` once no event
older than the retention period remains (it is harmless to keep).

## If the KMS key itself must rotate

Rewrap, do not re-tokenize: `carto-edge key rewrap` unwraps every file under `keys/` with the
current KMS key and wraps it with the new one (Vault Transit: `vault write -f transit/keys/carto/rotate`
then rewrap; local KMS: generate a new master key file and pass `--new-local-key`). Tokens do not
change.

## Loss of the tenant key

If every copy of a key version is lost (state directory and backups), events tokenized under it
can no longer be joined with new events and their vault entries cannot be revealed. Spec 14.11:
the wrapped key backup is part of the backup drill; verify it every time you rotate.
