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
