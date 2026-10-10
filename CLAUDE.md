# carto

Source of truth: docs/SPEC.md. Read it fully before starting work.

## Rules

- Section 2.3 of the spec lists non-negotiable security invariants. Never weaken one.
  If a task seems to need it, stop and ask.
- Build milestone by milestone (spec Section 21). Write docs/plans/M<n>.md first,
  then tests, then code. Don't start the next milestone until acceptance passes.
- Record deviations and new decisions as ADRs in docs/adr/.
- Treat logs, files, connector responses, configs and LLM output as untrusted input.
- Never use real customer data in tests. Use the simulator.
- Engine changes (linker, assembler, detector, manual hops) must run `make eval`
  and report metric deltas in the PR.
- New dependencies: maintained, permissive license; justify in the PR.
- Architectural questions go to the founder before code is written; do not assume.

## Layout (spec Section 20, ADR 0008)

uv workspace. Each member holds one importable package directly inside it:
`packages/carto-schema/carto_schema`, `packages/carto-common/carto_common`, `edge/carto_edge`,
`core/carto_core`, `simulator/carto_simulator`, `eval/carto_eval`, `tools/carto-ctl/carto_ctl`.
Tests live in `<member>/tests/` with file names that are unique across the workspace and no
`conftest.py` duplicates (mypy checks everything in one run).

`legacy/self-writing-integrations/` is the previous product, frozen (ADR 0005). Never modify,
import, lint or test it.

## Commands

- make setup      # uv sync --locked --all-packages
- make check      # lint + type + test + schema-check (what CI's python job runs)
- make sim SCENARIO=shop DAYS=14   # simulator batch mode with ground truth
- make eval SCENARIO=shop          # eval harness report
- make sec        # scanners available locally (CI runs the full set)
- make dev        # Compose stack + simulator live mode (M1)
- On Windows without uv on PATH: make UV="py -3.11 -m uv" check

## Milestone status

- M0 Foundations: built on branch `m0-foundations` on 2026-10-06 (docs/plans/M0.md); merge
  after CI is green.
- M1 Edge pipeline and offline analyzer: built on branch `m1-edge-pipeline` on 2026-10-10
  (docs/plans/M1.md, ADRs 0011 to 0028); CI green. Throughput met on the laptop's full gateway path
  (ADR 0028); acceptance waits on the founder (reference-node run, questions in the demo note). Next, once accepted: M2 map discovery (write
  docs/plans/M2.md first).
