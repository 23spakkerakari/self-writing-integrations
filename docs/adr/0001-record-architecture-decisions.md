# ADR 0001: Record architecture decisions

Status: accepted, 2026-10-06

## Context

The spec (`docs/SPEC.md`, Section 0.1, item 5) requires every decision that deviates from or
extends it to be recorded with context, decision, alternatives and consequences.

## Decision

Architecture decision records live in `docs/adr/NNNN-title.md`, numbered in order of
acceptance, written in this format. A record is never edited after acceptance except to change
its status; a later record supersedes it. Pull requests that introduce a deviation link the ADR.

## Alternatives

- Decisions in the plan files only: harder to find later and mixed with milestone noise.
- Decisions in commit messages: not browsable.

## Consequences

Every reviewer can see why the code differs from the spec. The spec remains the source of truth;
ADRs are the diff against it.
