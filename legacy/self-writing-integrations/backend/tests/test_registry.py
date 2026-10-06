import pytest

from app.registry.store import NotFound, RegistryError


def test_version_lifecycle(registry, manifest):
    record = registry.create_version(manifest, provenance="test")
    assert record.status == "draft"
    assert registry.next_version("bamboohr") == "0.1.1"

    with pytest.raises(RegistryError, match="only verified"):
        registry.publish("bamboohr", "0.1.0")

    registry.record_verification("bamboohr", "0.1.0", {"passed": True}, passed=True)
    published = registry.publish("bamboohr", "0.1.0")
    assert published.status == "published"
    assert published.published_at is not None
    assert registry.get_published("bamboohr").version == "0.1.0"


def test_publishing_supersedes_previous(registry, manifest):
    registry.create_version(manifest)
    registry.record_verification("bamboohr", "0.1.0", {}, passed=True)
    registry.publish("bamboohr", "0.1.0")

    v2 = manifest.model_copy(update={"version": "0.1.1"})
    registry.create_version(v2)
    registry.record_verification("bamboohr", "0.1.1", {}, passed=True)
    registry.publish("bamboohr", "0.1.1")

    statuses = {v.version: v.status for v in registry.list_versions("bamboohr")}
    assert statuses == {"0.1.0": "superseded", "0.1.1": "published"}
    summary = registry.list_integrations()[0]
    assert summary.published_version == "0.1.1" and summary.version_count == 2


def test_failed_verification_marks_rejected(registry, manifest):
    registry.create_version(manifest)
    record = registry.record_verification("bamboohr", "0.1.0", {"passed": False}, passed=False)
    assert record.status == "rejected"
    assert record.verification == {"passed": False}


def test_duplicate_version_rejected(registry, manifest):
    registry.create_version(manifest)
    with pytest.raises(RegistryError, match="already exists"):
        registry.create_version(manifest)


def test_unknown_lookups_raise_not_found(registry):
    with pytest.raises(NotFound):
        registry.get_version("nope", "0.0.1")
    with pytest.raises(NotFound):
        registry.get_published("nope")
    with pytest.raises(NotFound):
        registry.list_versions("nope")


def test_snapshot_hash_is_stable(registry):
    a = registry.save_snapshot("bamboohr", "openapi: 3.0.3")
    b = registry.save_snapshot("bamboohr", "openapi: 3.0.3")
    assert a == b
