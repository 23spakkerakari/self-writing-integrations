"""Engine output as the eval harness scores it (spec 18.4; plan M0, "Eval predictions").

From M2 on an engine run hands the harness one directory. Each file is optional: a missing file
leaves that part of the prediction empty, so an M2 engine that only discovers links is scored on
links alone. Every model mirrors its ground-truth counterpart in
:mod:`carto_simulator.ground_truth`, which is why the truth itself is a valid prediction
(:meth:`Predictions.from_truth`).

| File | Model | One record per |
| --- | --- | --- |
| ``links.json`` | ``list[PredictedLink]`` | key link accepted or proposed, with its score |
| ``entities.json`` | ``list[PredictedEntity]`` | entity family (spec 9.6) |
| ``txn_membership.ndjson`` | :class:`MembershipRecord` | record placed in a transaction |
| ``batches.json`` | ``list[PredictedBatch]`` | batch key (spec 9.7) |
| ``alerts.json`` | ``list[PredictedAlert]`` | alert opened during the run (spec 10) |
| ``manual_hops.json`` | ``list[PredictedManualHop]`` | hop with its manual score (spec 11.1) |

A membership record maps a locator key to a transaction id, or to ``null`` when the record
stands alone.

Files are UTF-8. Alert ids must be unique within ``alerts.json`` (the metrics are keyed by
them) and membership keys within ``txn_membership.ndjson``.

Membership keys are ground-truth locator keys (ADR 0006). The engine knows events by the
``event_id`` the edge assigned; the edge's eval-mode ``locator_map.ndjson`` (M1) maps each locator
key to that ``event_id``, and whoever drives the engine run translates memberships back to locator
keys before handing them over. No locator ever reaches core.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from carto_simulator.ground_truth import (
    EntityField,
    EventTruth,
    FaultKind,
    FaultTruth,
    FieldRef,
    GroundTruth,
    LinkRole,
    LinkType,
)

LINKS_FILE = "links.json"
ENTITIES_FILE = "entities.json"
MEMBERSHIP_FILE = "txn_membership.ndjson"
BATCHES_FILE = "batches.json"
ALERTS_FILE = "alerts.json"
MANUAL_HOPS_FILE = "manual_hops.json"

PREDICTION_FILES: tuple[str, ...] = (
    LINKS_FILE,
    ENTITIES_FILE,
    MEMBERSHIP_FILE,
    BATCHES_FILE,
    ALERTS_FILE,
    MANUAL_HOPS_FILE,
)


class PredictionsError(ValueError):
    """A prediction file exists but cannot be used; the message names the file and the place."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "prediction timestamps must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


class PredictedLink(_Model):
    """A key link the engine reports (spec 9.3 to 9.5), with the linker's score."""

    a: FieldRef
    form_a: str
    b: FieldRef
    form_b: str
    link_type: LinkType
    role: LinkRole
    score: float = Field(ge=0.0, le=1.0)
    rank: int | None = Field(
        default=None, description="Position in the engine's review queue; not scored."
    )


class PredictedEntity(_Model):
    """An entity family: the (field, form) members of one connected component (spec 9.6)."""

    entity_id: str
    fields: list[EntityField]


class MembershipRecord(_Model):
    """One line of ``txn_membership.ndjson``: a locator key and the transaction it was put in."""

    key: str
    txn_id: str | None


class PredictedBatch(_Model):
    """A batch key the assembler detected (spec 9.7) and the transactions linked to it."""

    key_value: str
    txn_ids: list[str] = Field(default_factory=list)


class PredictedAlert(_Model):
    """An alert the detector opened (spec 10.2 to 10.5)."""

    alert_id: str
    expectation_kind: str = Field(
        description="hop_deadline, schedule, volume, error_rate, freshness or schema_drift."
    )
    target: str = Field(description="The expectation target: a hop, node, source or file pattern.")
    opened_at: datetime
    resolved_at: datetime | None = None
    affected_txn_ids: list[str] = Field(default_factory=list)
    is_visibility_gap: bool = Field(
        default=False,
        description="True when the alert reports our own blind spot (spec 10.2), never a stall.",
    )
    likely_causes: list[str] = Field(
        default_factory=list, description="Cause kinds per spec 10.4, best first."
    )
    system_id: str | None = None

    @field_validator("opened_at", "resolved_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class PredictedManualHop(_Model):
    """A hop with its manual score (spec 11.1) and the reviewer's decision when there is one."""

    from_node: str
    to_node: str
    score: float = Field(ge=0.0, le=1.0)
    confirmed: bool | None = None


class Predictions(_Model):
    """Everything one engine run hands the harness; ``present`` is False for the empty one."""

    links: list[PredictedLink] = Field(default_factory=list)
    entities: list[PredictedEntity] = Field(default_factory=list)
    membership: dict[str, str | None] = Field(default_factory=dict)
    batches: list[PredictedBatch] = Field(default_factory=list)
    alerts: list[PredictedAlert] = Field(default_factory=list)
    manual_hops: list[PredictedManualHop] = Field(default_factory=list)
    present: bool

    @classmethod
    def empty(cls) -> Self:
        """What M0 scores: no engine yet, so nothing is predicted."""
        return cls(present=False)

    @classmethod
    def load(cls, directory: Path) -> Self:
        """Read a prediction directory; a missing file leaves that part empty.

        A directory that does not exist gives the empty prediction (``present`` False). A file
        that exists but is malformed raises :class:`PredictionsError` naming the file, the line
        or item, and the problem.
        """
        if not directory.is_dir():
            return cls.empty()
        alerts = _load_list(directory / ALERTS_FILE, PredictedAlert)
        _require_unique_alert_ids(directory / ALERTS_FILE, alerts)
        return cls(
            links=_load_list(directory / LINKS_FILE, PredictedLink),
            entities=_load_list(directory / ENTITIES_FILE, PredictedEntity),
            membership=_load_membership(directory / MEMBERSHIP_FILE),
            batches=_load_list(directory / BATCHES_FILE, PredictedBatch),
            alerts=alerts,
            manual_hops=_load_list(directory / MANUAL_HOPS_FILE, PredictedManualHop),
            present=True,
        )

    @classmethod
    def from_truth(cls, truth: GroundTruth, events: Iterable[EventTruth]) -> Self:
        """The perfect prediction: what an engine that knew the ground truth would hand in.

        Links carry score 1.0; membership is the true transaction of every record (``null``
        for noise); one alert per fault that expects one, opened at the fault start with the
        fault's expectation kind, cause kind and affected transactions; manual hops score 1.0
        when manual and 0.0 otherwise, with the reviewer decision matching.
        """
        links = [
            PredictedLink(
                a=link.a,
                form_a=link.form_a,
                b=link.b,
                form_b=link.form_b,
                link_type=link.link_type,
                role=link.role,
                score=1.0,
            )
            for link in truth.links
        ]
        entities = [
            PredictedEntity(entity_id=entity.entity_id, fields=list(entity.fields))
            for entity in truth.entities
        ]
        membership = {event.key: event.txn_id for event in events}
        batches = [
            PredictedBatch(key_value=batch.key_value, txn_ids=list(batch.txn_ids))
            for batch in truth.batches
        ]
        alerts = [alert_for_fault(fault) for fault in truth.faults if fault.expected_alert]
        manual_hops = [
            PredictedManualHop(
                from_node=hop.from_node,
                to_node=hop.to_node,
                score=1.0 if hop.manual else 0.0,
                confirmed=hop.manual,
            )
            for hop in truth.manual_hops
        ]
        return cls(
            links=links,
            entities=entities,
            membership=membership,
            batches=batches,
            alerts=alerts,
            manual_hops=manual_hops,
            present=True,
        )

    def write(self, directory: Path) -> None:
        """Write the six files (the reference writer for engine milestones and for tests)."""
        directory.mkdir(parents=True, exist_ok=True)
        _dump_json(directory / LINKS_FILE, self.links)
        _dump_json(directory / ENTITIES_FILE, self.entities)
        _dump_json(directory / BATCHES_FILE, self.batches)
        _dump_json(directory / ALERTS_FILE, self.alerts)
        _dump_json(directory / MANUAL_HOPS_FILE, self.manual_hops)
        with (directory / MEMBERSHIP_FILE).open("w", encoding="utf-8", newline="\n") as handle:
            for key, txn_id in self.membership.items():
                handle.write(MembershipRecord(key=key, txn_id=txn_id).model_dump_json())
                handle.write("\n")


def alert_for_fault(fault: FaultTruth) -> PredictedAlert:
    """The alert a perfect detector raises for ``fault`` (spec 19 expected detection)."""
    return PredictedAlert(
        alert_id=f"alert_{fault.fault_id}",
        expectation_kind=fault.expected_alert_kind or fault.kind.value,
        target=fault.source_id or fault.system_id,
        opened_at=fault.start,
        resolved_at=fault.end,
        affected_txn_ids=list(fault.affected_txn_ids),
        is_visibility_gap=fault.kind is FaultKind.VISIBILITY_GAP,
        likely_causes=[fault.expected_cause_kind] if fault.expected_cause_kind else [],
        system_id=fault.system_id,
    )


def _first_problem(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {error['msg']}" if location else error["msg"]


def _read_json(path: Path) -> object:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{path}: line {exc.lineno} column {exc.colno}: {exc.msg}"
        raise PredictionsError(msg) from exc
    except UnicodeDecodeError as exc:
        msg = f"{path}: not UTF-8: {exc.reason}"
        raise PredictionsError(msg) from exc
    except RecursionError as exc:
        msg = f"{path}: JSON nested too deeply"
        raise PredictionsError(msg) from exc
    except OSError as exc:
        msg = f"{path}: cannot read: {exc.strerror or exc}"
        raise PredictionsError(msg) from exc
    return payload


def _load_list[T: BaseModel](path: Path, model: type[T]) -> list[T]:
    if not path.is_file():
        return []
    payload = _read_json(path)
    if not isinstance(payload, list):
        msg = f"{path}: expected a JSON array of {model.__name__} objects"
        raise PredictionsError(msg)
    items: list[T] = []
    for index, item in enumerate(payload):
        try:
            items.append(model.model_validate(item))
        except ValidationError as exc:
            msg = f"{path}: item {index}: {_first_problem(exc)}"
            raise PredictionsError(msg) from exc
    return items


def _load_membership(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    membership: dict[str, str | None] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = MembershipRecord.model_validate_json(line)
                except ValidationError as exc:
                    msg = f"{path}: line {line_no}: {_first_problem(exc)}"
                    raise PredictionsError(msg) from exc
                if record.key in membership:
                    msg = f"{path}: line {line_no}: duplicate key {record.key!r}"
                    raise PredictionsError(msg)
                membership[record.key] = record.txn_id
    except UnicodeDecodeError as exc:
        msg = f"{path}: not UTF-8: {exc.reason}"
        raise PredictionsError(msg) from exc
    except OSError as exc:
        msg = f"{path}: cannot read: {exc.strerror or exc}"
        raise PredictionsError(msg) from exc
    return membership


def _require_unique_alert_ids(path: Path, alerts: Iterable[PredictedAlert]) -> None:
    """The metrics key alerts by id (matched alert, false alerts), so a repeat is an error."""
    seen: set[str] = set()
    for index, alert in enumerate(alerts):
        if alert.alert_id in seen:
            msg = f"{path}: item {index}: duplicate alert_id {alert.alert_id!r}"
            raise PredictionsError(msg)
        seen.add(alert.alert_id)


def _dump_json(path: Path, items: Iterable[BaseModel]) -> None:
    payload = [item.model_dump(mode="json") for item in items]
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
