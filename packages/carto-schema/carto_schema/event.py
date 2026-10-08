"""Canonical event: the edge-to-core contract (spec 7.1).

Every record the edge forwards is one :class:`CanonicalEvent`. High-cardinality values never
appear in it in clear (spec 2.3 invariant 2): identifiers are HMAC tokens that carry only a shape
and a length, ``template_text`` holds the template constants only, and ``attributes`` holds only
fields classified as low-cardinality and non-sensitive. Unknown keys are rejected so a newer edge
cannot leak a field an older core would store without looking at it. Timestamps are UTC with
millisecond precision.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from carto_schema.forms import Form, Shape, Token

SchemaVersion = Literal["1"]
"""The ``schema_version`` value this package speaks (spec 7.1)."""

SCHEMA_VERSION: Final[SchemaVersion] = "1"

MAX_IDENTIFIERS_PER_EVENT: Final = 64
MAX_ATTRIBUTE_VALUE_LEN: Final = 256
MAX_ATTRIBUTES: Final = 256
MAX_TEMPLATE_TEXT_LEN: Final = 4096
MAX_DROPPED_FIELDS: Final = 1024
MAX_ATTRIBUTE_KEY_LEN: Final = 128
MAX_FIELD_NAME_LEN: Final = 256

ULID_PATTERN: Final = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
"""A ULID in its canonical form: 26 upper-case Crockford base32 characters, first one 0 to 7."""

TENANT_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SOURCE_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""Shared by ``source_id`` and ``system_id``."""
TEMPLATE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

Ulid = Annotated[str, Field(pattern=ULID_PATTERN.pattern)]
TenantId = Annotated[str, Field(pattern=TENANT_ID_PATTERN.pattern)]
SourceId = Annotated[str, Field(pattern=SOURCE_ID_PATTERN.pattern)]
SystemId = Annotated[str, Field(pattern=SOURCE_ID_PATTERN.pattern)]
TemplateId = Annotated[str, Field(pattern=TEMPLATE_ID_PATTERN.pattern)]
AttributeKey = Annotated[str, Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY_LEN)]
AttributeValue = Annotated[str, Field(max_length=MAX_ATTRIBUTE_VALUE_LEN)]
FieldName = Annotated[str, Field(min_length=1, max_length=MAX_FIELD_NAME_LEN)]


def _require_text_or_datetime(value: object) -> object:
    """Reject numeric timestamps: the contract is ISO 8601 text (a datetime in Python mode)."""
    if isinstance(value, str | datetime):
        return value
    msg = "timestamp must be an ISO 8601 string or a datetime"
    raise ValueError(msg)


def _to_utc_millis(value: datetime) -> datetime:
    """Convert an aware datetime to UTC and truncate it to millisecond precision."""
    try:
        in_utc = value.astimezone(UTC)
    except OverflowError as exc:
        msg = "timestamp out of range after conversion to UTC"
        raise ValueError(msg) from exc
    return in_utc.replace(microsecond=in_utc.microsecond // 1000 * 1000)


def _format_utc_millis(value: datetime) -> str:
    """Render a UTC datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (spec 7.1 example)."""
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
        f".{value.microsecond // 1000:03d}Z"
    )


UtcTimestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_require_text_or_datetime),
    AfterValidator(_to_utc_millis),
    PlainSerializer(_format_utc_millis, when_used="json"),
]
"""A timestamp field (spec 7.1): ISO 8601 text or a datetime, must be timezone-aware (naive
input is rejected), stored in UTC truncated to milliseconds, serialized in JSON mode as
``YYYY-MM-DDTHH:MM:SS.mmmZ``. In Python mode (``model_dump()``) it stays a :class:`datetime`.
Numbers are rejected so the model agrees with the exported schema (``type: string``)."""


class EventKind(StrEnum):
    """What the edge observed (spec 7.1 ``kind``)."""

    LOG = "log"
    ROW_CHANGE = "row_change"
    FILE_ARRIVED = "file_arrived"
    FILE_REMOVED = "file_removed"
    HTTP_ACCESS = "http_access"
    WEBHOOK = "webhook"


class ObservedAtQuality(StrEnum):
    """Where ``observed_at`` came from (spec 7.1)."""

    SOURCE = "source"
    INGEST = "ingest"
    INFERRED = "inferred"


class ActorKind(StrEnum):
    """Who or what performed the action (spec 7.1 ``actor.kind``)."""

    HUMAN = "human"
    SERVICE = "service"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    """Log severity, when the source has one (spec 7.1 ``severity``)."""

    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


class ContractModel(BaseModel):
    """Base for every contract model.

    Unknown keys are rejected. Attribute assignment is rejected (``frozen``); the ``list`` and
    ``dict`` fields stay ordinary containers, so a copy that was changed in place must be
    validated again before it is forwarded. Validation errors never echo the rejected input in
    ``str()`` or ``repr()``: a rejected value may be the clear text this contract exists to keep
    out of core's logs (spec 2.3 invariant 7). ``ValidationError.errors()`` and ``.json()`` still
    carry ``input`` unless called with ``include_input=False``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Identifier(ContractModel):
    """One form of one identifier-class field, tokenized (spec 7.1, 8.4).

    ``shape`` and ``len`` describe the form value the token was computed from, not the raw
    record value, so a ``digits.0`` entry of ``SO-0004471`` has shape ``9999`` and length 4.
    ``shape`` must be what :func:`carto_schema.forms.shape` returns, so a raw value cannot travel
    in the shape slot (spec 2.3 invariant 2); ``len`` is a strict integer (no booleans, no
    numeric strings), as in the exported schema.
    """

    field: FieldName
    form: Form
    token: Token
    shape: Shape
    len: Annotated[int, Field(ge=1, strict=True)]


class Actor(ContractModel):
    """Tokenized user or service identifier (spec 7.1 ``actor``)."""

    token: Token
    kind: ActorKind


class Redaction(ContractModel):
    """Which field policy produced the event and how much Presidio masked (spec 7.1, 8.3)."""

    policy_version: Annotated[str, Field(min_length=1, max_length=32)]
    entities_masked: Annotated[int, Field(ge=0, strict=True)]


class CanonicalEvent(ContractModel):
    """The canonical event the edge sends to core (spec 7.1).

    Field order matches the JSON in the spec so ``model_dump_json()`` reproduces it key for key.
    ``template_text`` must contain constants only; that is enforced at the edge (spec 8.3 rule 7),
    not by this model, which can only bound its length.
    """

    schema_version: SchemaVersion
    event_id: Ulid
    tenant_id: TenantId
    source_id: SourceId
    system_id: SystemId
    kind: EventKind
    observed_at: UtcTimestamp
    ingested_at: UtcTimestamp
    observed_at_quality: ObservedAtQuality
    template_id: TemplateId
    template_text: Annotated[str, Field(max_length=MAX_TEMPLATE_TEXT_LEN)]
    severity: Severity | None = None
    attributes: Annotated[dict[AttributeKey, AttributeValue], Field(max_length=MAX_ATTRIBUTES)]
    identifiers: Annotated[list[Identifier], Field(max_length=MAX_IDENTIFIERS_PER_EVENT)]
    actor: Actor | None = None
    dropped_fields: list[FieldName] = Field(default_factory=list, max_length=MAX_DROPPED_FIELDS)
    redaction: Redaction

    @field_validator("identifiers")
    @classmethod
    def _unique_field_form_and_key_version(cls, identifiers: list[Identifier]) -> list[Identifier]:
        """Reject two entries for the same ``(field, form, key version)``.

        One token per form per field per key version: during a key rotation the edge
        dual-tokenizes (spec 8.4), so one field and form may carry a ``t1.`` and a ``t2.``
        token side by side, never two tokens under the same key version (ADR 0023).
        """
        seen: set[tuple[str, str, str]] = set()
        for identifier in identifiers:
            version = identifier.token.partition(".")[0]
            triple = (identifier.field, identifier.form, version)
            if triple in seen:
                msg = (
                    f"duplicate identifier for field {triple[0]!r}, form {triple[1]!r} and key "
                    f"version {triple[2]!r}"
                )
                raise ValueError(msg)
            seen.add(triple)
        return identifiers

    @classmethod
    def example(cls) -> CanonicalEvent:
        """The spec 7.1 example event, with full-length tokens where the spec writes ``...``."""
        return cls.model_validate(EXAMPLE_EVENT)


def _example_token(body: str) -> str:
    """Build a synthetic example token from its 22-character body.

    The bodies are made up (the spec abbreviates its tokens with ``...``); building them here
    keeps bandit's hard-coded-secret check (B105) from reading the example as a credential.
    """
    return f"t1.{body}"


EXAMPLE_EVENT: Final[dict[str, object]] = {
    "schema_version": "1",
    "event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0A",
    "tenant_id": "default",
    "source_id": "src_wms_db",
    "system_id": "sys_warehouse",
    "kind": "row_change",
    "observed_at": "2026-10-06T21:12:03.412Z",
    "ingested_at": "2026-10-06T21:12:09.020Z",
    "observed_at_quality": "source",
    "template_id": "tpl_4f1c9a",
    "template_text": "INSERT purchase_orders",
    "severity": None,
    "attributes": {"status": "CREATED", "warehouse_code": "DC-03"},
    "identifiers": [
        {
            "field": "po_num",
            "form": "raw",
            "token": _example_token("q8Jm0h3cR2VfZp4Lx9sT1w"),
            "shape": "99-999",
            "len": 6,
        },
        {
            "field": "po_num",
            "form": "alnum",
            "token": _example_token("Gk2Wq7nXf4Lr9bT0sYv3Ez"),
            "shape": "99999",
            "len": 5,
        },
        {
            "field": "order_ref",
            "form": "raw",
            "token": _example_token("Yd7Rm2Kp8Vx1Qs6Nc4Tw0H"),
            "shape": "AA-9999999",
            "len": 10,
        },
        {
            "field": "order_ref",
            "form": "digits.0",
            "token": _example_token("Pz1Lk5Xr8Mw2Bd6Qn9Ct3F"),
            "shape": "9999",
            "len": 4,
        },
    ],
    "actor": {"token": _example_token("Hh3Vq6Zt1Nm4Rk8Pw2Ls7D"), "kind": "human"},
    "dropped_fields": ["customer_name", "ship_to_address"],
    "redaction": {"policy_version": "3", "entities_masked": 0},
}
"""The spec 7.1 example as JSON-shaped data. Tokens are synthetic: the spec abbreviates them."""
