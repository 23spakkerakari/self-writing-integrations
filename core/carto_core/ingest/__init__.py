"""carto-core ingest-api (spec 5.3, 8.5, 12): receives batches from the edge over mutual TLS,
validates the contract models, writes to ClickHouse and records source health.

:mod:`carto_core.ingest.app` is the FastAPI application; :mod:`carto_core.ingest.ledger` the
batch idempotency ledger (ADR 0015); :mod:`carto_core.ingest.health` the source health store;
:mod:`carto_core.ingest.server` the uvicorn runner with the TLS settings of spec 14.4.
"""
