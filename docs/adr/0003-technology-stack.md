# ADR 0003: Technology stack

Status: accepted, 2026-10-06 (spec Section 6)

## Context

The spec fixes the stack; this record captures why, and the licenses of what M0 actually pulls
in, so later dependency additions follow the same bar (maintained, permissive license, justified
in the PR).

## Decision

Python 3.12 with FastAPI, Pydantic v2 and uvicorn for every service; `uv` workspace with a hashed
lockfile; ClickHouse for events and PostgreSQL 16+ for metadata and job leases; OpenTelemetry
Collector Contrib (configuration only) for log shipping; `drain3` for templates; Presidio for PII
detection; `google-re2` for customer-supplied patterns; `defusedxml`; `datasketch` and `ddsketch`;
`cryptography` only for crypto; `asyncssh`; `psycopg` 3, `pyodbc`, `PyMySQL` through SQLAlchemy 2
core with `sqlglot` statement validation; `clickhouse-connect`; `Authlib` for OIDC; `structlog`;
React + TypeScript + Vite with TanStack Query, React Flow, Radix and Tailwind; pytest, Hypothesis,
testcontainers, Vitest, Playwright, k6; ruff, mypy strict, Semgrep, Bandit, pip-audit,
osv-scanner, gitleaks, Trivy, Syft, cosign, OWASP ZAP; minimal non-root images, Compose and Helm.

Dependencies added in M0, with license and reason:

| Package | License | Why |
| --- | --- | --- |
| pydantic | MIT | canonical event and contract models, JSON Schema export |
| pydantic-settings | MIT | typed configuration with explicit schemas (Section 20) |
| structlog | MIT or Apache-2.0 | structured product logs with a redaction processor (Section 14.12) |
| hatchling (build) | MIT | boring PEP 517 backend for workspace members |
| pytest, pytest-cov | MIT | tests and coverage |
| hypothesis | MPL-2.0 | property tests required by Section 18.1; dev-only, never shipped, so the weak copyleft never attaches to product code |
| ruff, mypy | MIT | lint, format, strict types |
| bandit, pip-audit | Apache-2.0 | security CI (Section 6) |
| jsonschema | MIT | validate exported JSON Schemas and examples in tests |
| tzdata | Apache-2.0 | IANA time zone database for `zoneinfo`; Windows and minimal container images ship none, and calendars (spec 10.1) and the simulator need America/New_York with DST |

## Alternatives

Go for the edge (faster per node) was considered and deferred: the spec records a revisit point
at about 5k events/s on one node. Poetry or pip-tools instead of uv: slower, no hash-pinned
workspace support of the same quality.

## Consequences

Throughput targets in Section 17 are met with multiprocessing rather than a faster runtime.
Every new dependency is noted in its PR with license and reason.
