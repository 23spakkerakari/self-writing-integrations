# ADR 0028: Throughput: measure the full gateway path first, then run several pipeline workers

Status: accepted by the founder, 2026-10-10; step 1 done the same day, step 2 not triggered
(spec 17, spec 21 M1 acceptance, spec stack table).

## Context

M1 acceptance asks for 2,000 events/s sustained on the reference node (8 vCPU, 32 GB, the whole
stack on one Compose node). The edge pipeline runs in one Python process, so it uses one core.
`carto-edge bench` measured 1,100 to 2,400 events/s on the development laptop, depending on
load. It covers parsing, classification, tokenization and the reveal vault only. The live
gateway also updates field statistics on every record, appends to the disk buffer, commits
cursors and forwards batches, none of which that figure includes.

A profile of the measured loop shows no single hotspot: parsing about a third, tokenizing
identifiers about a third, assembling events about a quarter. The spec's stack table already
says Python is sufficient for v1 "with multiprocessing".

## Decision

The founder chose, in order:

1. **Measure the full gateway path.** `carto-edge bench --path gateway` runs the gateway-mode
   runtime (live statistics, quarantine, persisted templates), the real `Ingestor`, `DiskBuffer`
   and `CursorStore`, records fed in the scheduler's 500-record chunks, and the real `Forwarder`
   on its own thread posting every batch to a stub core that accepts it. The sustained figure
   counts only events the stub acknowledged, over the ingest time plus the time the forwarder
   needed to empty the buffer afterwards. Network and TLS to core are not included.
2. **If that falls short of 2,000 events/s, run several pipeline worker processes.** The
   design is a separate ADR, written before any code.

Alternatives considered: tuning the single process (no single hotspot, modest gains), an edge
rewrite in Go (the spec reserves it for partners above about 5,000 events/s), and lowering the
target for early partners (not chosen).

## Result, 2026-10-10

The earlier figures were wrong. The development laptop (Intel i7-1355U) has 2 performance cores
and 8 efficiency cores, and a single process runs on either. Those runs were also throttled,
most likely on battery power (it was on mains power and 34% charged when this was found). Each
run below is pinned to one logical processor with `start /affinity`, uses 20 seconds and loads
an equal share of records from all seven scenario A sources (the loader used to take only the
first two log files):

| Benchmark | Efficiency core | Performance core |
| --- | --- | --- |
| `make bench` (pipeline only) | 3,114 events/s | 5,435 events/s |
| `make bench-gateway` (sustained, buffer emptied) | 2,398 events/s | 3,600 events/s |

The full gateway path meets 2,000 events/s in one process even on the slow core, so step 2 is
not triggered and no worker design is started. The margin on a slow core is small (about 20%).
Server cores are usually closer to the performance core, but only a reference-node run shows it.

## Consequences

The gateway figure is the one M1 acceptance is judged on. CI runs both benchmarks and publishes
their figures as annotations (informative: shared runners). The reference-node run stays with
the founder. If a partner's volume or the reference node shows a shortfall, step 2 starts with
its design ADR.
