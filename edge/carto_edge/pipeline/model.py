"""Records as they move through the edge pipeline (spec 5.4, 8.2 to 8.4).

The shapes here are the contract between the pipeline stages, which live in separate modules
and were built by separate hands (plan M1, "Interfaces"):

- :class:`RawRecord`: what a connector hands over (spec 8.1). It carries the source position
  (``locator``, ADR 0006) from which ``event_id`` is derived, raw ``text`` for log lines or
  pre-parsed ``fields`` for rows, OTLP key-values, webhook bodies and file metadata, and the
  cursor to commit once the record is durable.
- :class:`ParsedRecord`: after parsing and template mining (spec 8.2): a flat ``fields``
  mapping of path to string value, the template, the source timestamp and its quality, the
  severity and the raw actor value. Still in clear; never leaves the edge.
- :class:`FieldClass`, :class:`Policy`, :class:`FieldDecision`: what the classifier decided
  for a field (spec 8.3, 5.4 step 4 and 5).
- :class:`VaultEntry` and :class:`PipelineResult`: what tokenization produced (spec 8.4): the
  canonical event to forward and the raw values to keep in the reveal vault.

``field_ref`` is ``<system_id>/<template_id>/<path>`` (spec 7.2 ``event_identifiers.field_ref``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from carto_schema.event import CanonicalEvent, EventKind, ObservedAtQuality, Severity

__all__ = [
    "FIELD_REF_SEPARATOR",
    "FieldClass",
    "FieldDecision",
    "ParsedRecord",
    "PipelineResult",
    "Policy",
    "RawRecord",
    "VaultEntry",
    "field_ref",
    "split_field_ref",
]

FIELD_REF_SEPARATOR: Final = "/"


def field_ref(system_id: str, template_id: str, path: str) -> str:
    """``<system_id>/<template_id>/<path>`` (spec 7.2)."""
    return f"{system_id}{FIELD_REF_SEPARATOR}{template_id}{FIELD_REF_SEPARATOR}{path}"


def split_field_ref(ref: str) -> tuple[str, str, str]:
    """Inverse of :func:`field_ref`; the path may itself contain ``/``."""
    system_id, _, rest = ref.partition(FIELD_REF_SEPARATOR)
    template_id, separator, path = rest.partition(FIELD_REF_SEPARATOR)
    if not system_id or not template_id or not separator or not path:
        msg = "not a field_ref: expected <system_id>/<template_id>/<path>"
        raise ValueError(msg)
    return system_id, template_id, path


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One record as read from a source, before parsing (spec 8.1).

    ``locator`` is the position inside the source (ADR 0006): ``<file name>:line:<n>`` for a
    line of a log file, ``<table or query>:row:<primary key>`` for a row, ``file:<name>`` for a
    file arrival or removal, ``otlp:<hash>`` and ``webhook:<hash>`` for pushed records (the
    hash of the body plus the receive time, since a push has no stable position). ``event_id``
    is ``derive_ulid(observed_at_ms, source_id, locator)`` so a record read twice gets the same
    id (spec 8.1, at-least-once delivery).

    Exactly one of ``text`` and ``fields`` is normally set: ``text`` for a log line (the parser
    picks the format), ``fields`` for a row, an OTLP key-value body, a webhook JSON body or file
    metadata. Both may be set when a structured record also carries a free-text message under
    a known key; the parser then mines the message for a template.

    ``commit_cursor`` is the connector cursor that may be committed once this record and every
    record before it are durably in the disk buffer (spec 8.1 "Cursor checkpointing"); it is
    ``None`` for records that do not advance the cursor. ``sequence`` orders records within a
    read for tie-breaking (spec 9.7 step 6).
    """

    source_id: str
    system_id: str
    kind: EventKind
    locator: str
    received_at: datetime
    text: str | None = None
    fields: Mapping[str, Any] | None = None
    sequence: int = 0
    commit_cursor: Mapping[str, Any] | None = None
    size_bytes: int = 0
    template_hint: str | None = None
    """Template text supplied by the connector for records that carry no message to mine:
    ``row_change <query name>``, ``file_arrived <generalized file name>`` (ADR 0016)."""


@dataclass(slots=True)
class ParsedRecord:
    """A record after parsing, template mining and timestamp extraction (spec 8.2).

    ``fields`` maps a flattened path (``payload.order.id``, ``@attr``, ``param_0``,
    ``msg.param_1``, a column name) to its string value; absent and null values are not
    present. ``template_text`` holds constants only. ``actor`` is the raw value of the
    configured actor field, tokenized later. ``observed_at`` is UTC.
    """

    source_id: str
    system_id: str
    kind: EventKind
    locator: str
    sequence: int
    received_at: datetime
    observed_at: datetime
    observed_at_quality: ObservedAtQuality
    template_id: str
    template_text: str
    fields: dict[str, str]
    severity: Severity | None = None
    actor: str | None = None
    parse_format: str = ""
    parse_notes: tuple[str, ...] = ()


class FieldClass(StrEnum):
    """Spec 8.3 classes; the PII group of rule 2 is split by kind so policy can differ."""

    IDENTIFIER = "identifier"
    LOW_CARD_ATTRIBUTE = "low_card_attribute"
    TIMESTAMP = "timestamp"
    AMOUNT = "amount"
    DATE = "date"
    PERSON_NAME = "person_name"
    CONTACT = "contact"
    GOVERNMENT_ID = "government_id"
    FINANCIAL = "financial"
    HEALTH = "health"
    FREE_TEXT = "free_text"
    SECRET_LIKE = "secret_like"  # noqa: S105 - a class name, not a credential
    UNKNOWN = "unknown"


class Policy(StrEnum):
    """What the edge does with a field's values (spec 5.4 step 5)."""

    KEEP = "keep"
    TOKENIZE = "tokenize"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class FieldDecision:
    """The classifier's verdict for one field (spec 8.3).

    ``forms`` names the forms to tokenize (spec 8.4 table) when the policy is ``tokenize``;
    empty otherwise. ``pinned`` is true when an admin policy pin (spec 8.3, Setup > Field
    policies) decided instead of the rules. ``reason`` is a short operator-facing note that
    never contains a value.
    """

    field_class: FieldClass
    policy: Policy
    forms: tuple[str, ...] = ()
    pinned: bool = False
    reason: str = ""
    samples_seen: int = 0


@dataclass(frozen=True, slots=True)
class VaultEntry:
    """A raw value to store in the reveal vault under its ``raw`` form token (spec 8.4)."""

    token: str
    raw_value: str
    expires_at: datetime


@dataclass(slots=True)
class PipelineResult:
    """What one raw record became.

    ``event`` is ``None`` when the record was dropped, and ``dropped_reason`` says why
    (``unparseable``, ``too_large``, ``no_timestamp_and_no_ingest_fallback`` and so on). The
    locator travels with the result so the eval-mode locator map (ADR 0006) can be written
    without the pipeline knowing about eval.
    """

    locator: str
    event: CanonicalEvent | None
    vault_entries: list[VaultEntry] = field(default_factory=list)
    dropped_reason: str | None = None
