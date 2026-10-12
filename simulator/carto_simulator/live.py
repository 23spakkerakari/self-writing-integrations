"""Live mode (spec 19 "Live mode"): a batch run replayed into a directory in real time.

The batch generator stays the only source of data. :func:`plan_live` generates the requested run
into a temporary directory with :func:`carto_simulator.api.generate`, reads the native files and
the ground truth back, shifts every timestamp by one constant so that the first record lands at
``now``, and discards the temporary directory. :func:`write_static_files` writes the shifted
``ground_truth/`` (locator keys, counts, links, batches and markers are those of the batch run),
the SQL seed and a ``README.md``. :func:`replay` then appends each record to the same per-day
file names the batch writer uses, at ``speed`` simulated seconds per real second, flushing after
every record so a tailing collector sees lines promptly:

- log lines go to ``webstore/app-*.ndjson``, ``orders/order-svc-*.log``,
  ``payments/messages-*.xml``, ``warehouse/export-job-*.log`` and
  ``shipping/shipping-app-*.log`` with the shifted timestamp rendered in the source's own
  convention (skew included);
- ``SHIP_*.csv`` files land in ``shipping/outbound/`` at their arrival instant with the mtime
  set to it;
- warehouse rows go to ``warehouse/purchase_orders.ndjson`` as they are created, one JSON
  object per row with the final state the batch export carries (the M1 Compose stack has no
  source database, so the edge reads the stream as a file); ``purchase_orders.sql`` is written
  whole up front, as the batch writer makes it, with the shifted timestamps.

Nothing here reads the wall clock: the caller passes ``now`` and the pacing clock. Counts and
file names are the only things reported about records (spec 2.3 invariant 7): error messages
name a source and a locator, never a value.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import IO, Final, Literal

from carto_simulator import clock
from carto_simulator.api import GENERATOR_VERSION, GenerationRequest, generate
from carto_simulator.formats import (
    iso_local_offset_ms,
    iso_utc_ms,
    naive_local_seconds,
    ndjson_line,
    sql_insert,
    sql_string,
)
from carto_simulator.ground_truth import (
    GROUND_TRUTH_DIR,
    MANIFEST_FILE,
    EventTruth,
    FaultTruth,
    GroundTruth,
    Manifest,
    iter_events,
    read_ground_truth,
    write_ground_truth,
)
from carto_simulator.scenarios import shop_truth
from carto_simulator.scenarios.shop_model import (
    ALTER_RENAME,
    CREATE_TABLE,
    CSV_COLUMNS,
    N_ORDER_CREATED,
    PO_TABLE,
    RENAMED_CSV_COLUMNS,
    SRC_ORDERS,
    SRC_PAYMENTS,
    SRC_SHIP_LOG,
    SRC_SHIP_SFTP,
    SRC_WEBSTORE,
    SRC_WMS_DB,
    SRC_WMS_EXPORT,
    WAREHOUSE_SKEW,
)

__all__ = [
    "DEFAULT_LIVE_DAYS",
    "DEFAULT_SPEED",
    "LIVE_SCENARIOS",
    "ROW_STREAM_FILE",
    "LiveError",
    "LivePlan",
    "ReplayItem",
    "ReplayStats",
    "plan_live",
    "replay",
    "write_static_files",
]

LIVE_SCENARIOS: Final[tuple[str, ...]] = ("shop",)
DEFAULT_LIVE_DAYS: Final = 2
DEFAULT_SPEED: Final = 60.0
"""Simulated seconds per real second: one simulated minute per real second."""
MAX_SLEEP_SECONDS: Final = 0.5
"""Longest single pause, so a stop request or the duration cap is honoured promptly."""

WAREHOUSE_DIR: Final = "warehouse"
FILE_DROP_DIR: Final = "shipping/outbound"
ROW_STREAM_FILE: Final = f"{WAREHOUSE_DIR}/{PO_TABLE}.ndjson"
SQL_SEED_FILE: Final = f"{WAREHOUSE_DIR}/{PO_TABLE}.sql"
_ROW_CSV_FILES: Final = (f"{PO_TABLE}.csv", f"{PO_TABLE}.renamed.csv")
_ROW_TIMESTAMP_COLUMNS: Final = frozenset({"created_at", "updated_at"})
_ROW_DATE_COLUMN: Final = "order_date"
_ROW_PRIMARY_KEY: Final = "id"
_SQL_UNQUOTED: Final = frozenset({"id", "order_total"})

ItemKind = Literal["line", "row", "file"]
StoppedBy = Literal["complete", "duration", "stop"]


class LiveError(ValueError):
    """The request cannot be replayed; the message names no record value."""


@dataclass(frozen=True, slots=True)
class ReplayItem:
    """One record to write: where, when (shifted true time) and the exact text."""

    observed_at: datetime
    source_id: str
    relative_path: str
    line_no: int
    kind: ItemKind
    content: str = field(repr=False)  # record text: never in a repr, log or message
    key: str


@dataclass(frozen=True)
class LivePlan:
    """Everything a replay needs, computed once and deterministic given ``now``."""

    request: GenerationRequest
    out_dir: Path
    anchor: datetime
    delta: timedelta
    items: tuple[ReplayItem, ...] = field(repr=False)
    events: tuple[EventTruth, ...] = field(repr=False)
    truth: GroundTruth = field(repr=False)
    batch_manifest: Manifest = field(repr=False)
    sql_text: str = field(repr=False)

    @property
    def first_at(self) -> datetime:
        return self.items[0].observed_at

    @property
    def last_at(self) -> datetime:
        return self.items[-1].observed_at

    @property
    def simulated_span(self) -> timedelta:
        return self.last_at - self.first_at

    def real_seconds(self, speed: float) -> float:
        """How long the full replay takes at ``speed`` simulated seconds per real second."""
        return self.simulated_span.total_seconds() / speed


@dataclass
class ReplayStats:
    planned: int
    written: int = 0
    lines: int = 0
    rows: int = 0
    files: int = 0
    simulated_at: datetime | None = None
    stopped_by: StoppedBy = "complete"

    def count(self, item: ReplayItem) -> None:
        self.written += 1
        if item.kind == "line":
            self.lines += 1
        elif item.kind == "row":
            self.rows += 1
        else:
            self.files += 1
        self.simulated_at = item.observed_at


# ---------------------------------------------------------------------------------------------
# Scenario A layout: how each source renders its clock (must match scenarios/shop.py)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LogLayout:
    """Where a log source's files live and how its timestamp appears inside a line."""

    source_id: str
    directory: str
    render: Callable[[datetime], str]
    before: str
    after: str
    skew: timedelta = timedelta(0)

    def stamp(self, observed_at: datetime) -> str:
        return self.before + self.render(observed_at + self.skew) + self.after

    def shift(self, line: str, observed_at: datetime, delta: timedelta, key: str) -> str:
        """``line`` with its rendered timestamp replaced by the shifted one."""
        original = self.stamp(observed_at)
        shifted = self.stamp(observed_at + delta)
        if not self.before:
            if not line.startswith(original):
                raise LiveError(_no_timestamp(key))
            return shifted + line[len(original) :]
        if original not in line:
            raise LiveError(_no_timestamp(key))
        return line.replace(original, shifted, 1)


def _no_timestamp(key: str) -> str:
    return f"record {key} does not carry the timestamp the ground truth records for it"


def _shop_log_layouts(zone: tzinfo) -> dict[str, _LogLayout]:
    def local_offset(instant: datetime) -> str:
        return iso_local_offset_ms(instant.astimezone(zone))

    def local_naive(instant: datetime) -> str:
        return naive_local_seconds(instant.astimezone(zone))

    layouts = (
        _LogLayout(SRC_WEBSTORE, "webstore", iso_utc_ms, '"ts": "', '"'),
        _LogLayout(SRC_ORDERS, "orders", iso_utc_ms, "ts=", " "),
        _LogLayout(SRC_PAYMENTS, "payments", local_offset, "<timestamp>", "</timestamp>"),
        _LogLayout(SRC_WMS_EXPORT, WAREHOUSE_DIR, local_naive, "", " ", WAREHOUSE_SKEW),
        _LogLayout(SRC_SHIP_LOG, "shipping", iso_utc_ms, "ts=", " "),
    )
    return {layout.source_id: layout for layout in layouts}


@dataclass(frozen=True, slots=True)
class _Row:
    row_id: int
    columns: tuple[str, ...]
    values: dict[str, str]
    renamed: bool


def _read_rows(directory: Path) -> tuple[dict[str, _Row], bool]:
    """The purchase-order rows by primary key, and whether the rename fault was active."""
    rows: dict[str, _Row] = {}
    rename_active = False
    for index, name in enumerate(_ROW_CSV_FILES):
        path = directory / name
        if not path.is_file():
            continue
        renamed = index == 1
        rename_active = rename_active or renamed
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            for record in reader:
                values = {column: record[column] for column in columns}
                pk = values[_ROW_PRIMARY_KEY]
                rows[pk] = _Row(int(pk), columns, values, renamed)
    return rows, rename_active


def _shift_row(
    row: _Row, delta: timedelta, zone: tzinfo, order_created_at: datetime | None
) -> dict[str, object]:
    """The row with its naive local timestamps and its order date moved by ``delta``."""
    shifted: dict[str, object] = {}
    for column in row.columns:
        value = row.values[column]
        if column in _ROW_TIMESTAMP_COLUMNS:
            local = datetime.fromisoformat(value).replace(tzinfo=zone)
            moved = local.astimezone(UTC) + delta
            shifted[column] = naive_local_seconds(clock.to_local(moved, zone))
        elif column == _ROW_DATE_COLUMN:
            shifted[column] = _shift_date(value, delta, zone, order_created_at)
        elif column == _ROW_PRIMARY_KEY:
            shifted[column] = row.row_id
        else:
            shifted[column] = value
    return shifted


def _shift_date(
    value: str, delta: timedelta, zone: tzinfo, order_created_at: datetime | None
) -> str:
    """``order_date`` is the local date of the order's creation; move that instant."""
    if order_created_at is not None:
        return clock.to_local(order_created_at + delta, zone).date().isoformat()
    midnight = clock.local_wall(date.fromisoformat(value), zone=zone).astimezone(UTC)
    return clock.to_local(midnight + delta, zone).date().isoformat()


def _sql_seed(
    rows: Iterable[dict[str, object]], renamed_flags: Iterable[bool], *, rename_active: bool
) -> str:
    """``purchase_orders.sql`` exactly as :mod:`scenarios.shop` writes it, from shifted rows."""
    statements: list[str] = [CREATE_TABLE]
    renamed_statements: list[str] = []
    for values, renamed in zip(rows, renamed_flags, strict=True):
        columns = RENAMED_CSV_COLUMNS if renamed else CSV_COLUMNS
        literals = [
            str(values[column]) if column in _SQL_UNQUOTED else sql_string(str(values[column]))
            for column in columns
        ]
        target = renamed_statements if renamed else statements
        target.append(sql_insert(PO_TABLE, columns, literals))
    if rename_active:
        statements.append(ALTER_RENAME)
        statements.extend(renamed_statements)
    return "\n".join(statements) + "\n"


# ---------------------------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------------------------


def plan_live(request: GenerationRequest, out_dir: Path, *, now: datetime) -> LivePlan:
    """Generate ``request`` once (batch mode, in a temporary directory) and plan its replay.

    ``now`` is where the first record lands (to the nearest second); it must be timezone-aware.
    The plan is a pure function of the request and ``now``. ``out_dir`` is recorded, not
    touched.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "now must be a timezone-aware datetime"
        raise LiveError(msg)
    if request.scenario not in LIVE_SCENARIOS:
        supported = ", ".join(LIVE_SCENARIOS)
        msg = f"live mode supports scenario {supported}; got {request.scenario!r}"
        raise LiveError(msg)
    with tempfile.TemporaryDirectory(prefix="carto-sim-live-", ignore_cleanup_errors=True) as tmp:
        staging = Path(tmp) / "batch"
        generate(request, staging)
        return _plan_from_batch(request, staging, out_dir, now.astimezone(UTC))


def _plan_from_batch(
    request: GenerationRequest, staging: Path, out_dir: Path, now: datetime
) -> LivePlan:
    batch_events = list(iter_events(staging))
    if not batch_events:
        msg = "the generated run holds no record"
        raise LiveError(msg)
    truth = read_ground_truth(staging)
    first = min(event.observed_at for event in batch_events)
    # A whole number of seconds keeps second-precision clocks exact; the first record lands
    # within half a second of ``now``, at ``anchor``.
    delta = timedelta(seconds=round((now - first).total_seconds()))
    anchor = first + delta
    zone = clock.local_zone()
    layouts = _shop_log_layouts(zone)
    rows, rename_active = _read_rows(staging / WAREHOUSE_DIR)
    order_created = {
        event.txn_id: event.observed_at
        for event in batch_events
        if event.node == N_ORDER_CREATED and event.txn_id is not None
    }
    file_lines: dict[Path, list[str]] = {}
    items: list[ReplayItem] = []
    shifted_rows: dict[int, dict[str, object]] = {}
    for event in batch_events:
        source_id, _, rest = event.key.partition(":")
        at = event.observed_at + delta
        layout = layouts.get(source_id)
        if layout is not None:
            file_name, _, number = rest.rpartition(":line:")
            path = staging / layout.directory / file_name
            lines = file_lines.get(path)
            if lines is None:
                lines = file_lines[path] = path.read_text(encoding="utf-8").splitlines()
            line_no = int(number)
            text = layout.shift(lines[line_no - 1], event.observed_at, delta, event.key)
            relative = f"{layout.directory}/{file_name}"
            items.append(ReplayItem(at, source_id, relative, line_no, "line", text, event.key))
        elif source_id == SRC_WMS_DB:
            pk = rest.rpartition(":row:")[2]
            row = rows.get(pk)
            if row is None:
                msg = f"record {event.key} has no row in the batch export"
                raise LiveError(msg)
            created = order_created.get(event.txn_id) if event.txn_id is not None else None
            values = _shift_row(row, delta, zone, created)
            shifted_rows[row.row_id] = values
            text = ndjson_line(values)
            items.append(ReplayItem(at, source_id, ROW_STREAM_FILE, 0, "row", text, event.key))
        elif source_id == SRC_SHIP_SFTP:
            name = rest.removeprefix("file:")
            content = (staging / FILE_DROP_DIR / name).read_text(encoding="utf-8")
            relative = f"{FILE_DROP_DIR}/{name}"
            items.append(ReplayItem(at, source_id, relative, 0, "file", content, event.key))
        else:
            msg = f"record {event.key} belongs to a source live mode does not know"
            raise LiveError(msg)
    items.sort(
        key=lambda item: (item.observed_at, item.source_id, item.relative_path, item.line_no)
    )
    ordered_ids = sorted(shifted_rows)
    sql_text = _sql_seed(
        (shifted_rows[row_id] for row_id in ordered_ids),
        (rows[str(row_id)].renamed for row_id in ordered_ids),
        rename_active=rename_active,
    )
    hashes = _planned_hashes(items, sql_text)
    shifted_events = tuple(
        event.model_copy(update={"observed_at": event.observed_at + delta})
        for event in batch_events
    )
    window_start = clock.local_midnight_utc(truth.manifest.start_date, zone)
    manifest = truth.manifest.model_copy(
        update={
            "start_date": clock.to_local(window_start + delta, zone).date(),
            "counts": {**truth.manifest.counts, "files": len(hashes)},
            "sha256": hashes,
        }
    )
    shifted_truth = truth.model_copy(
        update={
            "manifest": manifest,
            "faults": [_shift_fault(fault, delta) for fault in truth.faults],
        }
    )
    return LivePlan(
        request=request,
        out_dir=out_dir,
        anchor=anchor,
        delta=delta,
        items=tuple(items),
        events=shifted_events,
        truth=shifted_truth,
        batch_manifest=truth.manifest,
        sql_text=sql_text,
    )


def _shift_fault(fault: FaultTruth, delta: timedelta) -> FaultTruth:
    end = None if fault.end is None else fault.end + delta
    return fault.model_copy(update={"start": fault.start + delta, "end": end})


def _planned_hashes(items: Iterable[ReplayItem], sql_text: str) -> dict[str, str]:
    """sha256 of every native file as it will be once the replay has completed."""
    digests: dict[str, hashlib._Hash] = {}
    for item in items:
        digest = digests.get(item.relative_path)
        if digest is None:
            digest = digests[item.relative_path] = hashlib.sha256()
        digest.update(item.content.encode("utf-8"))
        if item.kind != "file":
            digest.update(b"\n")
    hashes = {path: digest.hexdigest() for path, digest in digests.items()}
    hashes[SQL_SEED_FILE] = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()
    return dict(sorted(hashes.items()))


# ---------------------------------------------------------------------------------------------
# Static files: output directory, ground truth, SQL seed, README
# ---------------------------------------------------------------------------------------------


def _prepare_out_dir(out_dir: Path) -> None:
    """Create ``out_dir`` or empty a previous run; refuse anything else (as batch mode does)."""
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
        return
    if not out_dir.is_dir():
        msg = f"{out_dir} exists and is not a directory"
        raise ValueError(msg)
    children = sorted(out_dir.iterdir())
    if children and not (out_dir / GROUND_TRUTH_DIR / MANIFEST_FILE).is_file():
        msg = (
            f"refusing to empty {out_dir}: it is not empty and holds no "
            f"{GROUND_TRUTH_DIR}/{MANIFEST_FILE} from a previous run"
        )
        raise ValueError(msg)
    for child in children:
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def write_static_files(plan: LivePlan) -> None:
    """Prepare ``plan.out_dir`` and write ``ground_truth/``, the SQL seed and ``README.md``.

    The ground truth describes the complete run, so an interrupted replay leaves a valid
    (if partially replayed) directory whose manifest counts the whole run.
    """
    out_dir = plan.out_dir
    _prepare_out_dir(out_dir)
    write_ground_truth(out_dir, plan.truth, plan.events)
    (out_dir / WAREHOUSE_DIR).mkdir(parents=True, exist_ok=True)
    (out_dir / SQL_SEED_FILE).write_text(plan.sql_text, encoding="utf-8", newline="\n")
    (out_dir / FILE_DROP_DIR).mkdir(parents=True, exist_ok=True)
    zone = clock.local_zone()
    shop_truth.write_readme(out_dir, plan.truth, plan.truth.manifest.start_date, zone)
    with (out_dir / "README.md").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_live_readme_section(plan))


def _live_readme_section(plan: LivePlan) -> str:
    request = plan.request
    seconds = int(plan.delta.total_seconds())
    lines = [
        "",
        "## Live replay",
        "",
        (
            f"Replayed by `carto-sim live` {GENERATOR_VERSION} from the batch generation with "
            f"seed {request.seed}, {request.days} days from {request.start_date.isoformat()}, "
            f"{request.daily_volume} orders per weekday. Every timestamp is shifted by "
            f"{seconds} s ({plan.delta}) so that the first record landed at "
            f"{plan.anchor.isoformat()}; later records follow at the configured speed. File "
            "names, locator keys, counts, links, batches and markers are those of the batch "
            "run; `ground_truth/` carries the shifted times and `manifest.json` the shifted "
            "start date and the digests of the complete replayed files."
        ),
        "",
        (
            f"- `{ROW_STREAM_FILE}` is the warehouse row stream: one JSON object per purchase "
            "order, appended at the row's creation instant with the final state the batch "
            "export carries (the M1 Compose stack has no source database, so the edge reads "
            f"this file as rows). `{SQL_SEED_FILE}` is the batch writer's seed with the shifted "
            "timestamps; the CSV exports are not written in live mode."
        ),
        (
            f"- Files under `{FILE_DROP_DIR}/` land at their arrival instant with the mtime set "
            "to it. Log lines are appended to the batch run's per-day file names."
        ),
        (
            "- To score a completed replay: `carto-eval run --scenario shop "
            f"--days {request.days} --seed {request.seed} --daily-volume {request.daily_volume} "
            "--sim-out <this directory>`; the harness notes that the start date differs from "
            "the default and scores the run as it is."
        ),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------------------------


class _Writer:
    """Appends records to their files, flushing per record; drops files with their mtime."""

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir
        self._handles: dict[str, IO[str]] = {}

    def write(self, item: ReplayItem) -> None:
        path = self._out_dir / item.relative_path
        if item.kind == "file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item.content, encoding="utf-8", newline="\n")
            stamp = item.observed_at.timestamp()
            os.utime(path, (stamp, stamp))
            return
        handle = self._handles.get(item.relative_path)
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", newline="\n")
            self._handles[item.relative_path] = handle
        handle.write(item.content)
        handle.write("\n")
        handle.flush()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


def replay(
    plan: LivePlan,
    *,
    speed: float = DEFAULT_SPEED,
    duration_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] | None = None,
    progress: Callable[[ReplayStats], None] | None = None,
    progress_every_seconds: float = 30.0,
) -> ReplayStats:
    """Write the planned records at ``speed`` simulated seconds per real second.

    Stops early, cleanly, when ``duration_seconds`` of real time have passed or ``should_stop``
    returns True; a ``KeyboardInterrupt`` raised from ``sleep`` closes every file before it
    propagates. ``progress`` is called at most every ``progress_every_seconds``.
    """
    if speed <= 0:
        msg = "speed must be positive"
        raise LiveError(msg)
    if duration_seconds is not None and duration_seconds <= 0:
        msg = "duration_seconds must be positive"
        raise LiveError(msg)
    stats = ReplayStats(planned=len(plan.items))
    if not plan.items:
        return stats
    writer = _Writer(plan.out_dir)
    start = monotonic()
    last_report = start
    first = plan.first_at
    try:
        for item in plan.items:
            due = start + (item.observed_at - first).total_seconds() / speed
            while True:
                now = monotonic()
                if duration_seconds is not None and now - start >= duration_seconds:
                    stats.stopped_by = "duration"
                    return stats
                if should_stop is not None and should_stop():
                    stats.stopped_by = "stop"
                    return stats
                remaining = due - now
                if remaining <= 0:
                    break
                sleep(min(remaining, MAX_SLEEP_SECONDS))
            writer.write(item)
            stats.count(item)
            if progress is not None and now - last_report >= progress_every_seconds:
                last_report = now
                progress(stats)
    finally:
        writer.close()
    return stats
