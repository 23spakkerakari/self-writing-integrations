# ADR 0005: Pivot this repository to carto and keep the previous product under `legacy/`

Status: accepted, 2026-10-06

## Context

The spec assumes an empty repository. This repository held a different product
(self-writing integrations: manifest runtime, OAuth broker, drift repair, a developer console
and a landing page) with uncommitted hand-written milestone 6 edits. The founder chose to pivot
in place rather than start a second repository.

## Decision

The previous product moved, with history preserved through git renames, to
`legacy/self-writing-integrations/` as the first commit on branch `m0-foundations`. `main` is
untouched until that branch is merged. The uncommitted milestone 6 edits travel with the move and
remain uncommitted for the founder to review. `carto_v1.md` became `docs/SPEC.md`. The Section 20
layout now lives at the repository root. Nothing under `legacy/` is linted, typed, tested,
scanned or built by carto's CI; it is reference material only.

## Alternatives

- A new repository next to this one: cleanest match to the spec, but the founder preferred one
  history and one remote.
- Deleting the old product: it may still inform v2 (fix and connect), so it stays.

## Consequences

Scanners and tooling exclude `legacy/` explicitly (ruff, mypy, bandit, Semgrep, Trivy, Renovate).
The GitHub repository can be renamed to `carto` at any time; the remote URL is updated then.
