"""Container-backed checks (spec 18.2): migrations apply idempotently to a real ClickHouse and
PostgreSQL, a retention change rewrites the TTLs, a batch posted to the app lands as rows in
``events`` and ``event_identifiers`` with its ledger row, a duplicate is skipped, a heartbeat
upserts, a bundle loads idempotently, and a bundle the offline analyzer builds from scenario A
loads into core with no marker in the stored rows (spec 21, M1 acceptance; spec 18.3). Skipped
when Docker is unreachable."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import warnings
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import docker  # type: ignore[import-untyped]
import pytest
import sqlalchemy
import zstandard
from clickhouse_connect.driver.client import Client
from testcontainers.community.clickhouse import ClickHouseContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from carto_common.crypto import SigningKey, b64url_encode
from carto_common.ids import derive_ulid, new_ulid
from carto_common.settings import RetentionSettings
from carto_core.bundle import LoadResult, iter_events, load_bundle, verify_bundle
from carto_core.db.clickhouse import ClickHouseWriter, create_client
from carto_core.db.postgres import create_engine_from_settings
from carto_core.ingest.app import create_app
from carto_core.ingest.health import PostgresSourceHealthStore
from carto_core.ingest.ledger import PostgresBatchLedger
from carto_core.migrations import (
    apply_clickhouse_migrations,
    apply_postgres_migrations,
    apply_retention,
)
from carto_core.settings import ClickHouseSettings, CoreSettings, PostgresSettings
from carto_edge.cli.analyze import analyze
from carto_schema.bundle import (
    DATA_FILES,
    EVENTS_FILE,
    FIELDS_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
    SIGNATURE_FILE,
    TEMPLATES_FILE,
    BundleCounts,
    BundleManifest,
    BundleSignature,
    BundleSourceSummary,
    FileDigest,
)
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch, SourceHeartbeat, SourceStatus
from carto_simulator.api import GenerationRequest, generate

with warnings.catch_warnings():
    # starlette 1.7 deprecates httpx (vs httpx2) under its TestClient at import time.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

# Pinned defaults; CARTO_TEST_*_IMAGE lets a developer or CI point at an image already present.
CLICKHOUSE_IMAGE = os.environ.get(
    "CARTO_TEST_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:24.8"
)
POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"
JSON = {"content-type": "application/json"}
BUNDLE_ID = "01K71Y5B2XQ0M4N8P3R6S9T1VX"
BASE_MS = 1_790_000_000_000
ROOT = Path(__file__).resolve().parents[2]


def _docker_available() -> bool:
    try:
        client = docker.from_env()
    except Exception:
        return False
    try:
        client.ping()
    except Exception:
        return False
    finally:
        client.close()
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


@pytest.fixture(scope="module", autouse=True)
def _no_ryuk_on_windows() -> None:
    # Docker Desktop's named-pipe transport stalls on starting the Ryuk reaper; the ``with``
    # blocks below stop and remove the containers themselves. Linux CI keeps Ryuk.
    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True


@pytest.fixture(scope="module")
def clickhouse(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ClickHouseSettings]:
    password_file = tmp_path_factory.mktemp("secrets") / "clickhouse"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with ClickHouseContainer(
        CLICKHOUSE_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME
    ) as container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(8123))
        yield ClickHouseSettings(
            url=f"http://{host}:{port}",
            secure=False,
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            timeout_seconds=60,
        )


@pytest.fixture(scope="module")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresSettings]:
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with PostgresContainer(
        POSTGRES_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, driver=None
    ) as container:
        yield PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            sslmode="disable",
        )


@pytest.fixture(scope="module")
def migrated(
    clickhouse: ClickHouseSettings, postgres: PostgresSettings
) -> Iterator[tuple[Client, sqlalchemy.Engine]]:
    client = create_client(clickhouse)
    apply_clickhouse_migrations(client, RetentionSettings())
    engine = create_engine_from_settings(postgres)
    apply_postgres_migrations(engine)
    yield client, engine
    engine.dispose()
    client.close()


def _event(tag: str, index: int) -> CanonicalEvent:
    ms = BASE_MS + index
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(ms, tag, str(index)),
        "observed_at": datetime.fromtimestamp(ms / 1000, tz=UTC),
    }
    return CanonicalEvent.model_validate(data)


def _count(client: Client, table: str, event_ids: Sequence[str]) -> int:
    result = client.query(
        "SELECT count() FROM {table:Identifier} WHERE event_id IN {ids:Array(String)}",
        parameters={"table": table, "ids": list(event_ids)},
    )
    return int(result.result_rows[0][0])


def _write_bundle(path: Path, events: Sequence[CanonicalEvent]) -> None:
    path.mkdir()
    lines = b"".join(event.model_dump_json().encode("utf-8") + b"\n" for event in events)
    (path / EVENTS_FILE).write_bytes(zstandard.ZstdCompressor(level=3).compress(lines))
    (path / FIELDS_FILE).write_bytes(b"[]\n")
    (path / TEMPLATES_FILE).write_bytes(b"[]\n")
    (path / MANIFEST_MD_FILE).write_bytes(b"# integration bundle\n")
    files = {
        name: FileDigest(
            sha256=hashlib.sha256((path / name).read_bytes()).hexdigest(),
            bytes=(path / name).stat().st_size,
        )
        for name in DATA_FILES
    }
    count = len(events)
    manifest = BundleManifest(
        bundle_version="1",
        schema_version="1",
        bundle_id=BUNDLE_ID,
        tenant_id="default",
        created_at=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
        producer="integration test",
        key_versions=[1],
        policy_version="3",
        sources=[
            BundleSourceSummary(
                source_id="src_wms_db",
                system_id="sys_warehouse",
                connector_type="upload",
                records_read=count,
                events_written=count,
                records_dropped=0,
                parse_errors=0,
            )
        ],
        counts=BundleCounts(
            records_read=count,
            events=count,
            records_dropped=0,
            parse_errors=0,
            identifiers=4 * count,
            fields_kept=2,
            fields_tokenized=2,
            fields_dropped=2,
        ),
        files=files,
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    (path / MANIFEST_FILE).write_bytes(manifest_bytes)
    key = SigningKey.generate()
    signature = BundleSignature(
        algorithm="ed25519",
        key_id=key.verify_key.key_id,
        public_key=key.verify_key.to_text(),
        signature=b64url_encode(key.sign(manifest_bytes)),
        signed_at=manifest.created_at,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    (path / SIGNATURE_FILE).write_bytes(signature.model_dump_json(indent=2).encode("utf-8"))


def test_clickhouse_migrations_apply_once_and_retention_rewrites_ttls(
    clickhouse: ClickHouseSettings,
) -> None:
    client = create_client(clickhouse)
    try:
        first = apply_clickhouse_migrations(client, RetentionSettings())
        assert first in (["0001_events.sql", "0002_event_identifiers.sql"], [])
        assert apply_clickhouse_migrations(client, RetentionSettings()) == []
        ledger = client.query("SELECT name FROM schema_migrations ORDER BY name").result_rows
        assert [row[0] for row in ledger] == ["0001_events.sql", "0002_event_identifiers.sql"]
        tables = client.query(
            "SELECT name FROM system.tables WHERE database = currentDatabase()"
        ).result_rows
        assert {"events", "event_identifiers", "schema_migrations"} <= {row[0] for row in tables}
        assert apply_retention(client, RetentionSettings()) == []
        assert apply_retention(client, RetentionSettings(events_days=45)) == [
            "events",
            "event_identifiers",
        ]
        for table in ("events", "event_identifiers"):
            engine_full = client.query(
                "SELECT engine_full FROM system.tables "
                "WHERE database = currentDatabase() AND name = {name:String}",
                parameters={"name": table},
            ).result_rows[0][0]
            assert "toIntervalDay(45)" in engine_full, engine_full
        assert apply_retention(client, RetentionSettings(events_days=45)) == []
    finally:
        client.close()


def test_postgres_migrations_apply_idempotently(postgres: PostgresSettings) -> None:
    engine = create_engine_from_settings(postgres)
    try:
        apply_postgres_migrations(engine)
        apply_postgres_migrations(engine)
        inspector = sqlalchemy.inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "systems",
            "sources",
            "source_health",
            "ingest_batches",
            "alembic_version",
        } <= tables
        for table in ("systems", "sources", "source_health", "ingest_batches"):
            columns = {column["name"] for column in inspector.get_columns(table)}
            assert {"tenant_id", "created_at", "updated_at"} <= columns, table
        ledger_columns = {column["name"] for column in inspector.get_columns("ingest_batches")}
        assert ledger_columns == {
            "tenant_id",
            "batch_id",
            "source_id",
            "received_at",
            "event_count",
            "created_at",
            "updated_at",
        }
        with engine.connect() as connection:
            version = connection.execute(
                sqlalchemy.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "0001"
    finally:
        engine.dispose()


def test_batch_through_the_app_lands_in_clickhouse_and_the_ledger(
    migrated: tuple[Client, sqlalchemy.Engine],
    clickhouse: ClickHouseSettings,
    postgres: PostgresSettings,
) -> None:
    client, engine = migrated
    settings = CoreSettings(clickhouse=clickhouse, postgres=postgres)
    app = create_app(
        settings,
        ClickHouseWriter(client),
        PostgresBatchLedger(engine),
        PostgresSourceHealthStore(engine),
    )
    events = [_event("app", 0), _event("app", 1)]
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=datetime.now(UTC),
        events=events,
    )
    heartbeat = SourceHeartbeat(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        sent_at=datetime.now(UTC),
        status=SourceStatus.DEGRADED,
        last_success_at=datetime.now(UTC),
        lag_seconds=12.5,
        error_count=2,
        buffer_depth=40,
        oldest_buffered_at=None,
        message="splunk export slow",
    )
    body = batch.model_dump_json().encode("utf-8")
    with TestClient(app) as test_client:
        assert test_client.get("/readyz").status_code == 200
        first = test_client.post("/internal/ingest", content=body, headers=JSON)
        assert first.status_code == 200, first.text
        assert first.json() == {"accepted": 2, "duplicate": False}
        second = test_client.post("/internal/ingest", content=body, headers=JSON)
        assert second.json() == {"accepted": 2, "duplicate": True}
        beat = test_client.post(
            "/internal/heartbeat", content=heartbeat.model_dump_json().encode(), headers=JSON
        )
        assert beat.status_code == 200, beat.text
    ids = [event.event_id for event in events]
    assert _count(client, "events", ids) == 2
    assert _count(client, "event_identifiers", ids) == 8
    identifier_rows = client.query(
        "SELECT field_ref, form, shape FROM event_identifiers WHERE event_id = {id:String} "
        "ORDER BY field_ref, form",
        parameters={"id": ids[0]},
    ).result_rows
    assert [tuple(row) for row in identifier_rows] == [
        ("sys_warehouse/tpl_4f1c9a/order_ref", "digits.0", "9999"),
        ("sys_warehouse/tpl_4f1c9a/order_ref", "raw", "AA-9999999"),
        ("sys_warehouse/tpl_4f1c9a/po_num", "alnum", "99999"),
        ("sys_warehouse/tpl_4f1c9a/po_num", "raw", "99-999"),
    ]
    event_row = client.query(
        "SELECT kind, attributes, actor_token, actor_kind, severity, system_id FROM events "
        "WHERE event_id = {id:String}",
        parameters={"id": ids[0]},
    ).result_rows[0]
    assert tuple(event_row) == (
        "row_change",
        {"status": "CREATED", "warehouse_code": "DC-03"},
        "t1.Hh3Vq6Zt1Nm4Rk8Pw2Ls7D",
        "human",
        None,
        "sys_warehouse",
    )
    with engine.connect() as connection:
        event_count = connection.execute(
            sqlalchemy.text(
                "SELECT event_count FROM ingest_batches "
                "WHERE tenant_id = :tenant_id AND batch_id = :batch_id"
            ),
            {"tenant_id": "default", "batch_id": batch.batch_id},
        ).scalar_one()
    assert event_count == 2
    record = PostgresSourceHealthStore(engine).get("default", "src_wms_db")
    assert record is not None
    assert record.status == "degraded"
    assert record.lag_seconds == 12.5
    assert record.error_count == 2
    assert record.message == "splunk export slow"


def test_bundle_loads_into_clickhouse_idempotently(
    migrated: tuple[Client, sqlalchemy.Engine], tmp_path: Path
) -> None:
    client, engine = migrated
    events = [_event("bundle", index) for index in range(7)]
    path = tmp_path / "scenario.carto"
    _write_bundle(path, events)
    verified = verify_bundle(path)
    writer, ledger = ClickHouseWriter(client), PostgresBatchLedger(engine)
    assert load_bundle(verified, writer, ledger, chunk_size=3) == LoadResult(7, 3, 0)
    ids = [event.event_id for event in events]
    assert _count(client, "events", ids) == 7
    assert _count(client, "event_identifiers", ids) == 28
    assert load_bundle(verified, writer, ledger, chunk_size=3) == LoadResult(0, 3, 3)
    assert _count(client, "events", ids) == 7
    with engine.connect() as connection:
        chunks = connection.execute(
            sqlalchemy.text("SELECT count(*) FROM ingest_batches WHERE source_id = :source_id"),
            {"source_id": f"bundle_{BUNDLE_ID.lower()}"},
        ).scalar_one()
    assert chunks == 3


def _leak_scan_module() -> Any:
    """``tools/ci/leak_scan.py``, the scanner CI runs over the Compose stack's rows."""
    spec = importlib.util.spec_from_file_location(
        "leak_scan", ROOT / "tools" / "ci" / "leak_scan.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scenario_a_bundle_from_the_analyzer_loads_into_core(
    migrated: tuple[Client, sqlalchemy.Engine], tmp_path: Path
) -> None:
    client, engine = migrated
    sim = tmp_path / "shop"
    generate(GenerationRequest(days=2, daily_volume=20, seed=11), sim)
    out = tmp_path / "shop.carto"
    result = analyze(
        ROOT / "simulator" / "analyze.shop.yaml", sim, out, state_dir=tmp_path / "state"
    )
    expected = result.manifest.counts.events
    assert expected > 0
    verified = verify_bundle(out)
    writer, ledger = ClickHouseWriter(client), PostgresBatchLedger(engine)
    loaded = load_bundle(verified, writer, ledger)
    assert loaded.events_written == expected
    assert loaded.duplicates == 0
    again = load_bundle(verified, writer, ledger)
    assert again == LoadResult(0, loaded.chunks, loaded.chunks)

    event_ids = [event.event_id for event in iter_events(verified)]
    assert len(event_ids) == expected
    assert _count(client, "events", event_ids) == expected
    rows = tmp_path / "rows.ndjson"
    with rows.open("wb") as handle:
        for table in ("events", "event_identifiers"):
            handle.write(
                client.raw_query(
                    "SELECT * FROM {table:Identifier} WHERE event_id IN {ids:Array(String)} "
                    "FORMAT JSONEachRow",
                    parameters={"table": table, "ids": event_ids},
                )
            )

    leak_scan = _leak_scan_module()
    markers = leak_scan.Markers(
        json.loads((sim / "ground_truth" / "markers.json").read_text(encoding="utf-8"))
    )
    assert len(markers) > 0
    hits = leak_scan.scan_rows(rows, markers)
    assert not hits, f"markers in the ClickHouse rows: {dict(hits)}"
