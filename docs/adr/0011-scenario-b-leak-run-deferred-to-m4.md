# ADR 0011: The scenario B leak run waits for scenario B (M4)

Status: accepted, 2026-10-08 (spec 18.3, 21; spec 0.1 item 10)

## Context

Spec Section 21 lists "Simulator scenario B" as an M4 deliverable, and M1's acceptance criteria
say "leak test passes on scenarios A and B". Both cannot hold at once. Scenario B (six systems,
X12 file names, member portal lines with name and date of birth) is a milestone of work on its own.

## Decision

The leak test (`edge/tests/test_edge_leak.py`) is parametrized by scenario and runs on scenario A
in M1. Scenario B is built in M4 as Section 21 schedules it, and the leak run over B becomes part
of M4's acceptance. Scenario A already plants markers in names, e-mail addresses, postal
addresses and every identifier field, so the M1 run exercises the same leak paths B would.

## Alternatives

- Build scenario B in M1: doubles the simulator work in the milestone that already carries the
  whole edge; the M4 detector work would still have to revisit B's faults.
- Skip the leak test until B exists: unacceptable; it is the most important test in the spec.

## Consequences

M4's plan must include the B leak run. The founder is told in the M1 hand-over. The M1 leak test
also covers the gateway path (forwarded batches and ClickHouse rows) so that B only adds data,
not test code.
