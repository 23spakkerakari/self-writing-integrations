# ADR 0008: Flat Python package inside each workspace member

Status: accepted, 2026-10-06

## Context

Section 20 shows `edge/gateway/`, `edge/pipeline/`, `core/linker/` and so on as plain
directories. A Python distribution needs an importable top-level package name; `edge` and `core`
are too generic to import, and `uv` workspace members need a `pyproject.toml` each.

## Decision

Each member is a distribution with one importable package directly inside it:

| Member directory | Distribution | Package |
| --- | --- | --- |
| `packages/carto-schema/` | `carto-schema` | `carto_schema` |
| `packages/carto-common/` | `carto-common` | `carto_common` |
| `edge/` | `carto-edge` | `carto_edge` (`gateway`, `connectors`, `pipeline`, `cli`) |
| `core/` | `carto-core` | `carto_core` (`ingest`, `profiler`, `linker`, `assembler`, `detector`, `notifier`, `api`, `llm_gateway`) |
| `simulator/` | `carto-simulator` | `carto_simulator` |
| `eval/` | `carto-eval` | `carto_eval` |
| `tools/carto-ctl/` | `carto-ctl` | `carto_ctl` |

Spec paths map one level deeper: `edge/pipeline/tokenize*` is `edge/carto_edge/pipeline/tokenize*`.
`CODEOWNERS` and the Semgrep connector rule use the real paths. `core/migrations/` stays a plain
directory as in the spec because migrations are data, not an importable package.

## Alternatives

- `src/` layout per member: one more directory level for no benefit in a monorepo where every
  member is installed editable.
- One giant package: loses the edge/core separation that the trust boundary depends on.

## Consequences

Imports are `from carto_edge.pipeline import ...`; tests live in `<member>/tests/` with unique
file names so mypy can check them all in one run.
