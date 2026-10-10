# ADR 0024: The eval locator map is `<bundle>.locator_map.ndjson`, next to the bundle

Status: accepted, 2026-10-09 (ADR 0006; spec 8.1.1, 18.4)

## Context

ADR 0006 says the edge gains an eval-only option that writes a `locator_map.ndjson` (locator key
to `event_id`) "next to its output", and that no locator information is ever sent to core. The
analyzer writes its output as a directory, `<name>.carto/`, and `make analyze` puts that directory
under `sim-out/` beside the simulator run (`sim-out/shop` and `sim-out/shop.carto`). A file named
exactly `locator_map.ndjson` in `sim-out/` would be shared by every scenario and overwritten by
the next run; a file inside the bundle directory would make the bundle fail `verify_bundle`,
which refuses any entry the manifest does not list, and would ship the locators to core.

## Decision

With `--locator-map`, `carto-edge analyze` writes `<out>.locator_map.ndjson` in the parent of the
bundle directory: `sim-out/shop.carto` gets `sim-out/shop.carto.locator_map.ndjson`. One JSON
object per emitted event, `{"key": "<source_id>:<locator>", "event_id": "<ULID>"}`, in emission
order. The bundle directory holds only the six files `verify_bundle` allows. The eval harness
resolves the map by the bundle path it was given.

## Alternatives

- `sim-out/locator_map.ndjson`: collides across scenarios and is far from the bundle it belongs to.
- Inside the bundle: breaks integrity verification and would hand locators to core (ADR 0006).

## Consequences

Tooling that drives an engine run over a bundle derives the map path from the bundle path. A
customer never passes `--locator-map`; the install guide says so.
