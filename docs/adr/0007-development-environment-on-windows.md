# ADR 0007: Development on Windows: uv from pip, managed CPython 3.12, scanners that only run in CI

Status: accepted, 2026-10-06

## Context

The founder develops on Windows 11 with Git Bash and GNU Make. On 2026-10-06 the machine had
Python 3.11 on PATH, a stale launcher entry for 3.12 with no interpreter behind it, no `uv`, no
`gh`, a Docker CLI with no running daemon, and none of the Go-binary scanners. The founder
allowed installing `uv` and pip-installable tools in user scope.

## Decision

- `uv` is installed with `pip install --user uv` under Python 3.11 (it is only the host for the
  binary) and lives in `%APPDATA%\Python\Python311\Scripts`. `uv` downloads and manages
  CPython 3.12 for the workspace (`.python-version`), so the project never depends on a
  system interpreter.
- `make` targets take `UV=...` so they work when `uv` is not on PATH
  (`make UV="py -3.11 -m uv" test`).
- Semgrep, gitleaks, Trivy, Syft, cosign and osv-scanner run in GitHub Actions. `make sec`
  runs Bandit and pip-audit locally and skips the others with a message when they are absent.
- `.gitattributes` normalizes every text file to LF so Makefiles, shell scripts and lockfiles are
  identical on Windows and Linux CI.
- Integration tests that need Docker (M1 onward) are marked `integration` and skipped locally
  when the daemon is unreachable; CI runs them.

## Alternatives

- The official uv installer: also fine, but it edits the user PATH, which the founder had not
  asked for.
- WSL: not available in the session.

## Consequences

Local runs cover lint, types, unit and property tests, the simulator, the eval harness, Bandit
and pip-audit. "CI green including scanners" is verified by pushing the milestone branch.
