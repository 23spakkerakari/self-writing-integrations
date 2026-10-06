from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.routes import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.manifest.schema import IntegrationManifest, load_manifest_file  # noqa: E402
from app.registry.store import Registry  # noqa: E402

MANIFEST_PATH = ROOT / "manifests" / "bamboohr.yaml"
SPEC_PATH = ROOT / "specs" / "bamboohr-employees.openapi.yaml"


@pytest.fixture
def manifest() -> IntegrationManifest:
    return load_manifest_file(str(MANIFEST_PATH))


@pytest.fixture
def manifest_dict(manifest: IntegrationManifest) -> dict:
    return manifest.model_dump(mode="json")


@pytest.fixture
def spec_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(f"sqlite:///{tmp_path / 'registry.db'}")


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", gateway_mode="mock")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_sleep():
    return lambda seconds: None


def has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# --- milestone 3: a published integration, a drifting world and the repair machinery ----------

import random  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

from app.db import Database  # noqa: E402
from app.drift.changes import ChangeRequests  # noqa: E402
from app.drift.monitor import DriftMonitor  # noqa: E402
from app.drift.repair import RepairPipeline, World  # noqa: E402
from app.manifest.schema import load_manifest  # noqa: E402
from app.runtime.gateway import CallResult, Connection, Gateway  # noqa: E402
from app.runtime.secrets import DictSecretsProvider  # noqa: E402
from app.verification.drift_scenarios import DriftScenario, DriftWorld  # noqa: E402
from app.verification.harness import verify  # noqa: E402


@dataclass
class DriftEnv:
    db: Database
    registry: Registry
    monitor: DriftMonitor
    changes: ChangeRequests
    manifest: IntegrationManifest
    connection: Connection
    secrets: DictSecretsProvider
    name: str = "bamboohr"

    def published(self) -> IntegrationManifest:
        return load_manifest(self.registry.get_published(self.name).manifest)

    def world(self, *mutations: Any, list_size: int = 3) -> DriftWorld:
        """The API as it behaves now: pinned to the published manifest plus the mutations."""
        return DriftWorld(self.published(), DriftScenario(mutations=list(mutations)), list_size=list_size)

    def call(self, world: DriftWorld, endpoint: str = "list_employees", params: dict | None = None, version: str | None = None) -> CallResult:
        record = self.registry.get_version(self.name, version) if version else self.registry.get_published(self.name)
        with Gateway(load_manifest(record.manifest), self.connection, self.secrets, transport=world.transport(), sleep=lambda s: None) as gateway:
            return gateway.call(endpoint, params or {})

    def repair_world(self, world: DriftWorld) -> World:
        return World(connection=self.connection, secrets=self.secrets, transport=world.transport(), sleep=lambda s: None)

    def pipeline(self, repair_fn: Any, max_rounds: int = 2) -> RepairPipeline:
        return RepairPipeline(self.registry, self.monitor, self.changes, repair_fn=repair_fn, max_rounds=max_rounds)


@pytest.fixture
def drift_env(tmp_path: Path, manifest: IntegrationManifest) -> DriftEnv:
    db = Database(f"sqlite:///{tmp_path / 'drift.db'}")
    registry = Registry(db)
    monitor = DriftMonitor(db, registry)
    changes = ChangeRequests(db, registry, monitor, rng=random.Random(7), min_calls=3)
    registry.create_version(manifest)
    registry.record_verification(manifest.name, manifest.version, verify(manifest).model_dump(mode="json"), True)
    registry.publish(manifest.name, manifest.version)
    return DriftEnv(
        db=db,
        registry=registry,
        monitor=monitor,
        changes=changes,
        manifest=manifest,
        connection=Connection(config={"company_domain": "acme"}),
        secrets=DictSecretsProvider({"api_key": "dev-key"}),
    )
