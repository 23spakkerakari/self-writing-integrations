# ADR 0009: The web app scaffold arrives in M2, not M0

Status: accepted, 2026-10-06

## Context

Section 20 lists `web/` and M0 asks for the repository skeleton with CI for lint, types and
tests. The first screen the spec needs is the review queue in M2 (Map, Review). A Vite scaffold
in M0 would add a Node toolchain, a lockfile and a CI job that test nothing for two milestones.

## Decision

`web/` holds only a README in M0. M2 adds the Vite + React + TypeScript scaffold, ESLint,
Prettier, Vitest, Playwright with axe, and the `web` CI job, together with the review queue
screen. The founder's design direction for the console (recorded outside the spec) is applied
when the first screen is built.

## Alternatives

Scaffold now: more "complete" skeleton, but dead weight and dependency churn (Renovate) for
nothing testable.

## Consequences

M2's plan must include the web toolchain setup and the CI job.
