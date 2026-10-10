"""carto_edge.pipeline.pipeline: parse, classify, redact and tokenize end to end (spec 5.4
steps 2 to 6, 7.1, 18.1): deterministic event ids, no raw value for a tokenized or dropped field,
actor handling, drop reasons, the two-pass analyzer mode and counters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from carto_common.crypto import Keyring, TokenKey, generate_key
from carto_common.ids import derive_ulid, is_ulid
from carto_edge.config import (
    ClassifySettings,
    FieldPolicyPin,
    ParseConfig,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import RawRecord
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.pipeline import (
    REASON_CONTRACT,
    REASON_UNKNOWN_SOURCE,
    EdgePipeline,
    actor_kind,
)
from carto_edge.pipeline.stats import FieldStatsStore
from carto_edge.pipeline.templates import TemplateStore
from carto_edge.pipeline.tokenize import Tokenizer
from carto_schema.event import ActorKind, EventKind, ObservedAtQuality, Severity

RECEIVED = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
KEYRING = Keyring(active=TokenKey(1, generate_key()))


def sources_file(
    pins: list[FieldPolicyPin] | None = None, actor_field: str | None = None
) -> SourcesFile:
    return SourcesFile(
        systems=[
            SystemConfig(id="sys_webstore", name="Webstore"),
            SystemConfig(id="sys_warehouse", name="Warehouse"),
        ],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_webstore",
                type=SourceType.UPLOAD,
                config={"paths": ["x.ndjson"]},
                parse=ParseConfig(actor_field=actor_field),
            ),
            SourceConfig(
                id="src_wms",
                system="sys_warehouse",
                type=SourceType.UPLOAD,
                config={"paths": ["po.csv"], "kind": "rows", "table": "purchase_orders"},
                parse=ParseConfig(actor_field="created_by", timestamp_field="updated_at"),
            ),
        ],
        field_policies=pins or [],
    )


def make(
    *,
    quarantine: int = 200,
    pins: list[FieldPolicyPin] | None = None,
    actor_field: str | None = None,
    observe_on_process: bool = True,
) -> EdgePipeline:
    sources = sources_file(pins, actor_field)
    classify = ClassifySettings(quarantine_samples=quarantine, distinct_threshold=10)
    stats = FieldStatsStore(sample_size=64)
    detector = RegexDetector()
    classifier = Classifier(
        classify, sources.field_policies, detector, stats, policy_version=sources.policy_version
    )
    return EdgePipeline(
        sources=sources,
        templates=TemplateStore(),
        classifier=classifier,
        detector=detector,
        tokenizer=Tokenizer(KEYRING, "default"),
        tenant_id="default",
        retention_days=30,
        clock=lambda: RECEIVED,
        observe_on_process=observe_on_process,
    )


def raw_line(text: str, line: int = 1, source_id: str = "src_web") -> RawRecord:
    return RawRecord(
        source_id=source_id,
        system_id="sys_webstore",
        kind=EventKind.LOG,
        locator=f"app.ndjson:line:{line}",
        received_at=RECEIVED,
        text=text,
        sequence=line,
    )


def raw_row(fields: dict[str, Any], pk: str) -> RawRecord:
    return RawRecord(
        source_id="src_wms",
        system_id="sys_warehouse",
        kind=EventKind.ROW_CHANGE,
        locator=f"purchase_orders:row:{pk}",
        received_at=RECEIVED,
        fields=fields,
        template_hint="row_change purchase_orders",
    )


def web_line(i: int, **extra: Any) -> str:
    data = {
        "ts": f"2026-09-23T04:{i % 60:02d}:{(i * 7) % 60:02d}.000Z",
        "level": "info",
        "msg": "cart created",
        "cart_id": f"c-{88000 + i}",
        "items": i % 5 + 1,
        "region": ("us-east", "us-west", "eu-west")[i % 3],
    }
    data.update(extra)
    return json.dumps(data)


def test_event_id_is_derived_from_source_position() -> None:
    pipeline = make()
    line = web_line(1)
    first = pipeline.process(raw_line(line))
    second = make().process(raw_line(line))
    assert first.event is not None and second.event is not None
    assert first.event.event_id == second.event.event_id
    observed_ms = int(first.event.observed_at.timestamp() * 1000)
    assert first.event.event_id == derive_ulid(observed_ms, "src_web", "app.ndjson:line:1")
    assert is_ulid(first.event.event_id)
    # The same record read again (at-least-once delivery) gets the same id.
    again = pipeline.process(raw_line(line))
    assert again.event is not None and again.event.event_id == first.event.event_id


def test_streaming_mode_quarantines_and_tokenizes_identifier_shaped_values() -> None:
    pipeline = make()
    result = pipeline.process(raw_line(web_line(1)))
    event = result.event
    assert event is not None
    text = event.model_dump_json()
    assert "c-88001" not in text
    assert "cart_id" in {identifier.field for identifier in event.identifiers}
    assert event.kind is EventKind.LOG
    assert event.observed_at_quality is ObservedAtQuality.SOURCE
    assert event.ingested_at == RECEIVED
    assert event.template_text == "cart created"
    assert event.tenant_id == "default"
    assert event.redaction.policy_version
    assert event.actor is None
    # Vault entries exist only for raw-form tokens of the event.
    raw_tokens = {i.token for i in event.identifiers if i.form == "raw"}
    assert {entry.token for entry in result.vault_entries} == raw_tokens
    assert all(entry.expires_at > event.observed_at for entry in result.vault_entries)
    assert pipeline.counters.events == 1 and pipeline.counters.records == 1


def test_complete_statistics_keep_low_cardinality_and_tokenize_identifiers() -> None:
    pipeline = make(quarantine=1, observe_on_process=False)
    for i in range(60):
        assert pipeline.observe_only(raw_line(web_line(i), line=i + 1))
    assert pipeline.counters.observed == 60
    result = pipeline.process(raw_line(web_line(3), line=4))
    event = result.event
    assert event is not None
    assert event.attributes["region"] == "us-east"
    assert event.severity is Severity.INFO  # the parser consumed the level field
    assert "cart_id" not in event.attributes
    fields = {identifier.field for identifier in event.identifiers}
    assert "cart_id" in fields
    forms = {identifier.form for identifier in event.identifiers if identifier.field == "cart_id"}
    # "c-88003" is already lower case, so its norm form equals raw and is not repeated (spec 8.4).
    assert forms == {"raw", "alnum", "digits.0"}
    assert "c-88003" not in event.model_dump_json()
    assert event.identifiers[0].shape == "A-99999"


def test_actor_field_is_tokenized_never_kept() -> None:
    pipeline = make(quarantine=1, observe_on_process=False)
    rows = [
        raw_row(
            {
                "id": str(i),
                "po_num": f"88-{200 + i}",
                "status": "CREATED",
                "created_by": "svc_wms_integration" if i % 2 else "jsmith",
                "updated_at": "2026-09-23 00:18:52",
            },
            pk=str(i),
        )
        for i in range(40)
    ]
    for row in rows:
        pipeline.observe_only(row)
    service = pipeline.process(rows[1]).event
    human = pipeline.process(rows[2]).event
    assert service is not None and human is not None
    assert service.actor is not None and service.actor.kind is ActorKind.SERVICE
    assert human.actor is not None and human.actor.kind is ActorKind.HUMAN
    assert service.actor.token != human.actor.token
    for event in (service, human):
        text = event.model_dump_json()
        assert "created_by" not in event.attributes
        assert "jsmith" not in text and "svc_wms_integration" not in text
        assert event.attributes["status"] == "CREATED"
        assert event.kind is EventKind.ROW_CHANGE
        assert event.template_text == "row_change purchase_orders"
        assert event.observed_at_quality is ObservedAtQuality.SOURCE


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("svc_wms_integration", ActorKind.SERVICE),
        ("api-gateway", ActorKind.SERVICE),
        ("order_bot", ActorKind.SERVICE),
        ("SYSTEM", ActorKind.SERVICE),
        ("cron.daemon", ActorKind.SERVICE),
        ("jsmith", ActorKind.HUMAN),
        ("Maria Lopez", ActorKind.HUMAN),
        ("   ", ActorKind.UNKNOWN),
    ],
)
def test_actor_kind(value: str, kind: ActorKind) -> None:
    assert actor_kind(value) is kind


def test_dropped_fields_are_named_not_valued() -> None:
    pins = [
        FieldPolicyPin(
            field="sys_webstore/*/customer_name", field_class="person_name", policy="drop"
        )
    ]
    pipeline = make(quarantine=1, pins=pins, observe_on_process=False)
    for i in range(30):
        pipeline.observe_only(raw_line(web_line(i, customer_name="Mei Reyes"), line=i + 1))
    event = pipeline.process(raw_line(web_line(2, customer_name="Mei Reyes"), line=3)).event
    assert event is not None
    assert "customer_name" in event.dropped_fields
    assert "Mei Reyes" not in event.model_dump_json()


def test_pinned_keep_of_a_file_name_is_still_hygiene_checked() -> None:
    pins = [
        FieldPolicyPin(field="sys_webstore/*/note", field_class="low_card_attribute", policy="keep")
    ]
    pipeline = make(quarantine=1, pins=pins)
    event = pipeline.process(raw_line(web_line(1, note="contact me at mei@example.com"))).event
    assert event is not None
    assert "note" not in event.attributes
    assert "note" in event.dropped_fields
    assert "example.com" not in event.model_dump_json()
    assert pipeline.counters.attributes_refused == 1


def test_drop_reasons_are_counted() -> None:
    pipeline = make()
    unknown = pipeline.process(
        RawRecord(
            source_id="src_nope",
            system_id="sys_webstore",
            kind=EventKind.LOG,
            locator="x:line:1",
            received_at=RECEIVED,
            text="hello",
        )
    )
    assert unknown.event is None and unknown.dropped_reason == REASON_UNKNOWN_SOURCE
    empty = pipeline.process(raw_line("   "))
    assert empty.event is None and empty.dropped_reason == "empty"
    tiny = pipeline.process(raw_line("x"))
    assert tiny.event is not None  # a one-byte line is a text record, not a failure
    assert pipeline.counters.dropped[REASON_UNKNOWN_SOURCE] == 1
    assert pipeline.counters.dropped["empty"] == 1
    assert pipeline.counters.dropped_total == 2
    assert pipeline.counters.records == 3


def test_contract_violation_is_dropped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = make()

    def bad_ulid(*_args: object, **_kwargs: object) -> str:
        return "not-a-ulid"

    monkeypatch.setattr("carto_edge.pipeline.pipeline.derive_ulid", bad_ulid)
    result = pipeline.process(raw_line(web_line(1)))
    assert result.event is None
    assert result.dropped_reason == REASON_CONTRACT
    assert pipeline.counters.dropped[REASON_CONTRACT] == 1


def test_template_text_is_redacted_and_counted() -> None:
    """A mined template masks digits and e-mail tokens before Drain3 sees them; a connector's
    template hint is the path where a constant could still carry PII (spec 8.3)."""
    pipeline = make()
    hinted = RawRecord(
        source_id="src_wms",
        system_id="sys_warehouse",
        kind=EventKind.ROW_CHANGE,
        locator="purchase_orders:row:7",
        received_at=RECEIVED,
        fields={"id": "7", "status": "NEW", "updated_at": "2026-09-23 00:18:52"},
        template_hint="row_change owner support@example.com",
    )
    event = pipeline.process(hinted).event
    assert event is not None
    assert "example.com" not in event.template_text
    assert "<EMAIL_ADDRESS>" in event.template_text
    assert event.redaction.entities_masked == 1
    mined = pipeline.process(
        raw_line(json.dumps({"ts": "2026-09-23T04:00:00Z", "msg": "notify x@example.com now"}))
    ).event
    assert mined is not None
    assert "example.com" not in mined.model_dump_json()


def test_timestamp_falls_back_to_ingest_time() -> None:
    pipeline = make()
    event = pipeline.process(raw_line(json.dumps({"msg": "no clock here", "k": "v"}))).event
    assert event is not None
    assert event.observed_at_quality is ObservedAtQuality.INGEST
    assert event.observed_at == RECEIVED


identifier_values = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
    min_size=6,
    max_size=24,
).filter(lambda s: any(c.isdigit() for c in s) and s[0].isalnum() and s[-1].isalnum())


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    values=st.lists(identifier_values, min_size=1, max_size=6, unique=True),
    names=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{1,10}_id", fullmatch=True), min_size=1, max_size=6
    ),
)
def test_property_no_raw_value_of_tokenized_or_dropped_field_in_event(
    values: list[str], names: list[str]
) -> None:
    """Spec 18.1: the pipeline never emits a raw value for a tokenized or dropped field."""
    pipeline = make()
    fields = dict(zip(names, values, strict=False))
    payload = {"ts": "2026-09-23T04:00:00Z", "msg": "order placed", **fields}
    event = pipeline.process(raw_line(json.dumps(payload))).event
    assert event is not None
    text = event.model_dump_json()
    kept = set(event.attributes)
    for name, value in fields.items():
        if name in kept:
            continue  # a kept value is low-cardinality and detector-clean by construction
        assert value not in text, name
        assert value.lower() not in text.lower(), name


@settings(max_examples=40, deadline=None)
@given(
    locator=st.from_regex(r"[a-z]{1,8}\.log:line:[1-9][0-9]{0,5}", fullmatch=True),
    ms=st.integers(min_value=0, max_value=2**48 - 1),
)
def test_property_derive_ulid_is_idempotent(locator: str, ms: int) -> None:
    first = derive_ulid(ms, "src_web", locator)
    assert first == derive_ulid(ms, "src_web", locator)
    assert is_ulid(first)
    assert first != derive_ulid(ms, "src_other", locator)
