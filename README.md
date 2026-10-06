# carto

Codename for an integration observability product: a read-only layer over the systems a company
already runs that maps how data moves between them, traces any business record across every
system it touched, watches for stalls and silent failures, and shows where a person is still
doing the talking. The one-line pitch: *"We show you how your systems talk to each other, and
where a person is still doing the talking."*

The build spec in [`docs/SPEC.md`](docs/SPEC.md) is the source of truth. Decisions that deviate
from or extend it are in [`docs/adr/`](docs/adr/). Milestone plans are in
[`docs/plans/`](docs/plans/).

## Status

M0 (foundations) in progress. See [`docs/plans/M0.md`](docs/plans/M0.md).

## Layout

```
packages/carto-schema/   canonical event and edge-to-core contract models, JSON Schema export
packages/carto-common/   settings, logging with redaction, ids
edge/                    carto-edge: connectors, pipeline, gateway, offline analyzer (M1)
core/                    carto-core: ingest, profiler, linker, assembler, detector, notifier, api
simulator/               scenario generators with ground truth (scenario A "shop" in M0)
eval/                    eval harness scoring engine output against ground truth
web/                     React UI (M2)
otel/                    OpenTelemetry Collector template (M1)
deploy/                  Compose (M1) and Helm (M6)
tools/carto-ctl/         operator CLI
docs/                    SPEC, ADRs, plans, threat model, runbooks, install guides
legacy/                  the previous product in this repository, frozen (ADR 0005)
```

## Quick start

Requires [`uv`](https://docs.astral.sh/uv/) and GNU make. uv downloads Python 3.12 itself.

```
make setup                       # install the locked workspace
make check                       # lint, types, tests, schema drift check
make sim SCENARIO=shop DAYS=14   # synthetic data for five systems plus ground truth in sim-out/shop
make eval SCENARIO=shop          # score engine output (M0: the empty prediction) in eval/reports
```

On Windows, run these from Git Bash. If `uv` is not on PATH: `make UV="py -3.11 -m uv" check`.

## Security

Spec Section 2.3 lists the invariants every line of code keeps. Report a vulnerability to the
owner privately; do not open a public issue.
