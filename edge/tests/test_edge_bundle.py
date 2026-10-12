"""carto_edge.bundle (spec 8.1.1, ADR 0022, 0024): the writer streams events into a bundle that
carto_core.bundle.verify_bundle accepts, records exact digests, signs the manifest bytes, holds
exactly six files, shows sample values for kept fields only, keeps the locator map outside the
bundle, refuses a non-empty output, and leaves nothing behind when a run fails."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import zstandard

from carto_common.crypto import VerifyKey, b64url_decode
from carto_common.ids import derive_ulid
from carto_core.bundle import iter_events, verify_bundle
from carto_edge.bundle import (
    BUNDLE_FILES,
    BundleWriteError,
    BundleWriter,
    default_locator_map_path,
)
from carto_edge.pipeline.model import PipelineResult
from carto_edge.pipeline.templates import TemplateRecord
from carto_schema.bundle import (
    DATA_FILES,
    EVENTS_FILE,
    FIELDS_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
    SIGNATURE_FILE,
    TEMPLATES_FILE,
    BundleField,
    BundleManifest,
    BundleSignature,
    ShapeShare,
)
from carto_schema.event import CanonicalEvent, EventKind

CREATED = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
BASE = datetime(2026, 10, 6, 21, 0, 0, tzinfo=UTC)
KEEP_SAMPLE = "DC-03"
TOKENIZED_SAMPLE = "SO-SAMPLE-LEAK"
SOURCES = {"src_wms_db": "upload", "src_web": "upload"}
SYSTEMS = {"src_wms_db": "sys_warehouse", "src_web": "sys_webstore"}


def event(
    index: int, *, source_id: str = "src_wms_db", template_id: str = "tpl_4f1c9a"
) -> CanonicalEvent:
    observed = BASE + timedelta(minutes=index)
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(int(observed.timestamp() * 1000), source_id, str(index)),
        "source_id": source_id,
        "system_id": SYSTEMS[source_id],
        "template_id": template_id,
        "observed_at": observed,
        "ingested_at": observed + timedelta(seconds=3),
    }
    return CanonicalEvent.model_validate(data)


def emitted(index: int, **kwargs: str) -> PipelineResult:
    return PipelineResult(locator=f"purchase_orders:row:{index}", event=event(index, **kwargs))


def dropped(reason: str, index: int = 99) -> PipelineResult:
    return PipelineResult(locator=f"file.log:line:{index}", event=None, dropped_reason=reason)


def bundle_field(path: str, policy: str, field_class: str, samples: list[str]) -> BundleField:
    return BundleField(
        field_ref=f"sys_warehouse/tpl_4f1c9a/{path}",
        system_id="sys_warehouse",
        template_id="tpl_4f1c9a",
        path=path,
        field_class=field_class,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
        reason="test" if policy == "drop" else "",
        count=12,
        distinct_estimate=3,
        null_rate=0.0,
        top_shapes=[ShapeShare(shape="AA-99", share=1.0)],
        forms=["raw", "norm"] if policy == "tokenize" else [],
        sample_values=samples,
    )


FIELDS = [
    bundle_field("warehouse_code", "keep", "low_card_attribute", [KEEP_SAMPLE, "DC-04"]),
    # A careless caller handing in samples for a tokenized field: the writer must not show them.
    bundle_field("order_ref", "tokenize", "identifier", [TOKENIZED_SAMPLE]),
    bundle_field("customer_name", "drop", "person_name", []),
]
TEMPLATES = [
    TemplateRecord(
        template_id="tpl_4f1c9a",
        system_id="sys_warehouse",
        template_text="INSERT purchase_orders",
        kind=EventKind.ROW_CHANGE,
        count=999,  # the store's lifetime count; the bundle counts its own events
        first_seen=BASE - timedelta(days=30),
        last_seen=BASE + timedelta(days=30),
    ),
    TemplateRecord(
        template_id="tpl_000000",
        system_id="sys_warehouse",
        template_text="stale template from an earlier run",
        kind=EventKind.LOG,
        count=5,
        first_seen=BASE,
        last_seen=BASE,
    ),
]


def writer(out_dir: Path, locator_map: Path | None = None) -> BundleWriter:
    return BundleWriter(
        out_dir,
        tenant_id="default",
        producer="carto-edge analyze test",
        key_versions=[1],
        policy_version="abc123",
        created_at=CREATED,
        locator_map=locator_map,
        clock=lambda: CREATED,
    )


def write_bundle(out_dir: Path, locator_map: Path | None = None) -> BundleManifest:
    with writer(out_dir, locator_map) as bundle:
        for index in range(1, 4):
            bundle.add(emitted(index), "src_wms_db")
        bundle.add(dropped("unparseable"), "src_wms_db")
        bundle.add(dropped("csv_header"), "src_wms_db")
        bundle.add(dropped("contract"), "src_wms_db")
        return bundle.finish(FIELDS, TEMPLATES, connector_types=SOURCES, system_of=SYSTEMS)


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    out = tmp_path / "shop.carto"
    write_bundle(out)
    return out


def test_bundle_verifies_and_streams_every_event(bundle_dir: Path) -> None:
    verified = verify_bundle(bundle_dir)
    events = list(iter_events(verified))
    assert [e.event_id for e in events] == [event(i).event_id for i in range(1, 4)]
    manifest = verified.manifest
    assert manifest.bundle_version == "1"
    assert manifest.tenant_id == "default"
    assert manifest.key_versions == [1]
    assert manifest.policy_version == "abc123"
    assert manifest.created_at == CREATED
    counts = manifest.counts
    assert (counts.records_read, counts.events, counts.records_dropped) == (6, 3, 3)
    assert counts.parse_errors == 2  # unparseable and csv_header; contract is not a parse error
    assert counts.identifiers == 3 * len(CanonicalEvent.example().identifiers)
    assert (counts.fields_kept, counts.fields_tokenized, counts.fields_dropped) == (1, 1, 1)
    assert manifest.first_observed_at == BASE + timedelta(minutes=1)
    assert manifest.last_observed_at == BASE + timedelta(minutes=3)


def test_sources_follow_the_configuration_and_include_sources_without_records(
    bundle_dir: Path,
) -> None:
    manifest = verify_bundle(bundle_dir).manifest
    assert [s.source_id for s in manifest.sources] == ["src_wms_db", "src_web"]
    wms, web = manifest.sources
    assert (wms.records_read, wms.events_written, wms.records_dropped) == (6, 3, 3)
    assert wms.system_id == "sys_warehouse"
    assert wms.connector_type == "upload"
    assert (web.records_read, web.events_written, web.first_observed_at) == (0, 0, None)


def test_exactly_six_files_and_digests_match(bundle_dir: Path) -> None:
    assert {entry.name for entry in bundle_dir.iterdir()} == BUNDLE_FILES
    assert len(BUNDLE_FILES) == 6
    manifest = BundleManifest.model_validate_json((bundle_dir / MANIFEST_FILE).read_bytes())
    assert set(manifest.files) == set(DATA_FILES)
    for name, digest in manifest.files.items():
        data = (bundle_dir / name).read_bytes()
        assert digest.sha256 == hashlib.sha256(data).hexdigest()
        assert digest.bytes == len(data)


def test_signature_covers_the_exact_manifest_bytes(bundle_dir: Path) -> None:
    manifest_bytes = (bundle_dir / MANIFEST_FILE).read_bytes()
    assert manifest_bytes.endswith(b"}\n")
    signature = BundleSignature.model_validate_json((bundle_dir / SIGNATURE_FILE).read_bytes())
    assert signature.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    key = VerifyKey.from_text(signature.public_key)
    assert key.key_id == signature.key_id
    key.verify(b64url_decode(signature.signature), manifest_bytes)
    assert signature.signed_at == CREATED


def test_each_run_signs_with_a_fresh_key(tmp_path: Path) -> None:
    write_bundle(tmp_path / "a.carto")
    write_bundle(tmp_path / "b.carto")
    first = json.loads((tmp_path / "a.carto" / SIGNATURE_FILE).read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "b.carto" / SIGNATURE_FILE).read_text(encoding="utf-8"))
    assert first["key_id"] != second["key_id"]


def test_events_file_is_one_zstd_json_line_per_event(bundle_dir: Path) -> None:
    data = (bundle_dir / EVENTS_FILE).read_bytes()
    with zstandard.ZstdDecompressor().stream_reader(data) as reader:
        text = reader.read().decode("utf-8")
    lines = text.splitlines()
    assert text.endswith("\n")
    assert [CanonicalEvent.model_validate_json(line) for line in lines] == [
        event(i) for i in range(1, 4)
    ]


def test_samples_appear_only_for_kept_fields(bundle_dir: Path) -> None:
    fields = json.loads((bundle_dir / FIELDS_FILE).read_text(encoding="utf-8"))
    assert [f["field_ref"] for f in fields] == sorted(f["field_ref"] for f in fields)
    by_path = {f["path"]: f for f in fields}
    assert by_path["warehouse_code"]["sample_values"] == [KEEP_SAMPLE, "DC-04"]
    assert by_path["order_ref"]["sample_values"] == []
    markdown = (bundle_dir / MANIFEST_MD_FILE).read_text(encoding="utf-8")
    assert f"`{KEEP_SAMPLE}`" in markdown
    for name in BUNDLE_FILES:
        assert TOKENIZED_SAMPLE not in (bundle_dir / name).read_bytes().decode("latin-1")


def test_manifest_md_lists_run_sources_fields_and_templates(bundle_dir: Path) -> None:
    markdown = (bundle_dir / MANIFEST_MD_FILE).read_text(encoding="utf-8")
    manifest = verify_bundle(bundle_dir).manifest
    assert markdown.startswith(f"# Carto bundle {manifest.bundle_id}\n")
    for heading in (
        "## Run",
        "## Signature",
        "## Sources",
        "## Fields kept in clear",
        "## Fields tokenized",
        "## Fields dropped",
        "## Templates",
    ):
        assert f"\n{heading}\n" in markdown
    kept = markdown.split("## Fields kept in clear")[1].split("## Fields tokenized")[0]
    tokenized = markdown.split("## Fields tokenized")[1].split("## Fields dropped")[0]
    dropped_part = markdown.split("## Fields dropped")[1].split("## Templates")[0]
    assert "warehouse_code" in kept
    assert "order_ref" in tokenized
    assert "`AA-99` 100%" in tokenized
    assert "raw, norm" in tokenized
    assert "customer_name" in dropped_part
    assert "person_name" in dropped_part
    assert "`src_wms_db`" in markdown


def test_templates_describe_the_bundle_events(bundle_dir: Path) -> None:
    templates = json.loads((bundle_dir / TEMPLATES_FILE).read_text(encoding="utf-8"))
    assert [t["template_id"] for t in templates] == ["tpl_4f1c9a"]  # the stale one is not listed
    only = templates[0]
    assert only["count"] == 3
    assert only["template_text"] == "INSERT purchase_orders"
    assert only["first_seen"] == "2026-10-06T21:01:00.000Z"
    assert only["last_seen"] == "2026-10-06T21:03:00.000Z"


def test_locator_map_lines_and_location(tmp_path: Path) -> None:
    out = tmp_path / "shop.carto"
    locator = default_locator_map_path(out)
    assert locator == tmp_path.resolve() / "shop.carto.locator_map.ndjson"
    write_bundle(out, locator)
    lines = [json.loads(line) for line in locator.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {"key": f"src_wms_db:purchase_orders:row:{i}", "event_id": event(i).event_id}
        for i in range(1, 4)
    ]
    verify_bundle(out)


def test_locator_map_inside_the_bundle_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "shop.carto"
    with pytest.raises(BundleWriteError, match="locator map"):
        writer(out, out / "locator_map.ndjson")
    assert not out.exists()


def test_non_empty_output_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "shop.carto"
    out.mkdir()
    (out / "keep.txt").write_text("customer file", encoding="utf-8")
    with pytest.raises(BundleWriteError, match="not an empty directory"):
        writer(out)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "customer file"
    a_file = tmp_path / "file.carto"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(BundleWriteError):
        writer(a_file)


def test_an_existing_empty_directory_is_used(tmp_path: Path) -> None:
    out = tmp_path / "shop.carto"
    out.mkdir()
    write_bundle(out)
    verify_bundle(out)


def test_a_failed_run_leaves_nothing_behind(tmp_path: Path) -> None:
    out = tmp_path / "shop.carto"
    locator = default_locator_map_path(out)
    with pytest.raises(RuntimeError, match="boom"), writer(out, locator) as bundle:
        bundle.add(emitted(1), "src_wms_db")
        raise RuntimeError("boom")
    assert not out.exists()  # the handles were closed, or Windows would refuse the removal
    assert not locator.exists()


def test_close_without_finish_releases_the_handles(tmp_path: Path) -> None:
    out = tmp_path / "shop.carto"
    bundle = writer(out)
    bundle.add(emitted(1), "src_wms_db")
    bundle.close()
    bundle.close()
    (out / EVENTS_FILE).unlink()  # would fail on Windows while a handle is open
    with pytest.raises(BundleWriteError, match="finished or closed"):
        bundle.add(emitted(2), "src_wms_db")


def test_finish_twice_and_foreign_events_are_refused(tmp_path: Path) -> None:
    with writer(tmp_path / "a.carto") as bundle:
        other_tenant = event(1).model_dump() | {"tenant_id": "other"}
        foreign = PipelineResult(
            locator="x:line:1", event=CanonicalEvent.model_validate(other_tenant)
        )
        with pytest.raises(BundleWriteError, match="tenant/source"):
            bundle.add(foreign, "src_wms_db")
        with pytest.raises(BundleWriteError, match="tenant/source"):
            bundle.add(emitted(1), "src_web")  # an event of src_wms_db filed under src_web
        bundle.finish([], [], connector_types=SOURCES, system_of=SYSTEMS)
        with pytest.raises(BundleWriteError, match="already finished"):
            bundle.finish([], [], connector_types=SOURCES, system_of=SYSTEMS)
    verify_bundle(tmp_path / "a.carto")


def test_a_source_without_a_system_is_refused(tmp_path: Path) -> None:
    with writer(tmp_path / "a.carto") as bundle:
        bundle.add(emitted(1), "src_wms_db")
        with pytest.raises(BundleWriteError, match="no system id"):
            bundle.finish([], [], connector_types={}, system_of={})


def test_naive_created_at_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BundleWriteError, match="timezone-aware"):
        BundleWriter(
            tmp_path / "a.carto",
            tenant_id="default",
            producer="p",
            key_versions=[1],
            policy_version="1",
            created_at=datetime(2026, 10, 9),
        )
