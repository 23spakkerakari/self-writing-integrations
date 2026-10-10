# carto-ctl

Operator CLI for a carto installation (spec Section 20, `tools/carto-ctl/`). M0 shipped the
command tree, the help text and the exit-code contract; M1 implements `pki init` and `key init`.
The other commands arrive with the milestones below; until then each one prints a single line
naming its milestone and spec section and exits with status 2, so a script can never mistake a
stub for success.

```
uv run carto-ctl --help
uv run carto-ctl version          # carto-ctl 0.1.0, exit 0
uv run carto-ctl pki init --out pki --ca-key-dir pki-ca --services ingest-api,edge-gateway,otel-collector
uv run carto-ctl key init --state-dir /var/lib/carto --tenant-id default --kms local
uv run carto-ctl audit verify     # carto-ctl audit verify: not implemented in this build; arrives in M6 (spec 14.9).   exit 2
```

`python -m carto_ctl ...` is equivalent to `carto-ctl ...`.

## Commands

| Command                       | Does                                                                        | Spec  | Milestone | This build |
| ----------------------------- | --------------------------------------------------------------------------- | ----- | --------- | ---------- |
| `carto-ctl pki init`          | Create the install-generated private CA for internal mutual TLS (Compose)   | 14.4  | M1        | works |
| `carto-ctl key init`          | Generate the 256-bit tenant tokenization key, stored only wrapped by the customer KMS or Vault Transit | 8.4 | M1 | works |
| `carto-ctl audit verify`      | Verify the audit log hash chain (`prev_hash`, `row_hash`) against its daily anchors | 14.9 | M6 | stub, exit 2 |
| `carto-ctl support-bundle`    | Write a redacted support bundle: versions, configuration without secrets, metric snapshots, recent product logs; no event data | 16 | M6 | stub, exit 2 |
| `carto-ctl drill restore`     | Run the scripted backup restore drill against a test stack                  | 14.11 | M6        | stub, exit 2 |
| `carto-ctl verify-signatures` | Verify cosign signatures and SLSA provenance of release images and SBOMs    | 14.8  | M6        | stub, exit 2 |
| `carto-ctl version`           | Print `carto-ctl <version>`                                                 | 20    | M0        | works, exit 0 |

### `pki init`

Writes `ca.crt` and one `<service>.crt`/`<service>.key` pair per service into `--out` (default
`pki`), and `ca.key` into `--ca-key-dir` (default: `--out`); keep the CA key where no running
service can read it. Options: `--services NAME[,NAME...]` (default
`edge-gateway,ingest-api,otel-collector,api`), `--dns NAME` and `--ip ADDR` (extra SANs on every
certificate, repeatable), `--days N` (leaf lifetime, default 90, spec 14.4) and `--if-missing`
(succeed without writing when every file is already there; a partial set is still refused).
Each leaf is valid for server and client authentication. Keys are owner-only and never
overwritten. The output lists certificate fingerprints, never key material.

### `key init`

Creates `<state-dir>/keys/` exactly as the edge `KeyManager` reads it: `tenant-key.json`
(version 1, wrapped), `rotation.json` and, with `--kms local`, the master key `local-kms.key`.
Options: `--state-dir` (default `/var/lib/carto-edge`), `--tenant-id` (default `default`),
`--kms local|vault`, `--vault-url`, `--vault-transit-key`, `--vault-mount`,
`--vault-token-file` (the token is read from a file, never an option or the environment) and
`--if-missing`. Existing key files are never overwritten. Rotation is
`docs/runbooks/key-rotation.md`.

`--help` on the root, on each group (`pki`, `key`, `audit`, `drill`) and on each command lists
what is available with the spec section it implements. `-V`/`--version` is the same as
`version`. Options are never abbreviated, at any level: `carto-ctl pki init --he` is a usage
error, not `--help`.

## Exit codes

| Code | Meaning                                                                                  |
| ---- | ---------------------------------------------------------------------------------------- |
| 0    | The command did what it says, or help was printed                                        |
| 1    | The command failed (a file it would overwrite exists, the KMS is unreachable)            |
| 2    | Usage error (unknown command or option), a group or no command given, or a stub          |

`main()` never raises `SystemExit`: argparse's own exits (0 for `--help`, 2 for usage errors)
are converted to return codes.

## Layout

```
carto_ctl/
  __init__.py     __version__
  __main__.py     python -m carto_ctl
  registry.py     Command (frozen dataclass: path, help, milestone, section, handler, configure),
                  Invocation, GROUPS, COMMANDS, RESERVED_DESTS, the stub and version handlers
  cli.py          build_parser() from the registry, main(argv) -> int, run(argv, commands)
  exit_codes.py   EXIT_OK, EXIT_FAILURE, EXIT_USAGE
  pki.py          pki init
  keyinit.py      key init
tests/
  test_ctl_cli.py, test_ctl_registry.py, test_ctl_pki.py, test_ctl_key.py
```

## Implementing a command

Each entry in `registry.COMMANDS` is a frozen `Command`. To implement one, replace its `handler`
(signature `(Invocation) -> int`; write to `invocation.out` / `invocation.err`, never `print`)
and, if it needs options, give it a `configure` hook (`(argparse.ArgumentParser) -> None`) that
adds them. Two namespace keys are reserved, `command` and `help_parser`
(`registry.RESERVED_DESTS`): the parser sets them on every command so `cli.run()` knows which
handler to call and whose help to print, and `build_parser()` raises `ValueError` for a hook
that adds an option with either `dest` or overrides either default. `cli.build_parser()`
generates the parser from the table, so nothing else changes; `Command.implemented` turns true
as soon as the handler is no longer `stub`, which also updates the long help. Real
implementations follow spec 2.3: no key material in arguments, environment variables or logs.

## Development

```
uv run pytest tools/carto-ctl/tests
uv run ruff check tools/carto-ctl && uv run ruff format --check tools/carto-ctl
uv run mypy
```
