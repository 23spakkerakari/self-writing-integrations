"""Per-source sinks that keep records in memory, write native files and count lines (ADR 0006).

Records are plain slotted dataclasses, not pydantic models, because the scenario creates one per
emitted line; :class:`~carto_simulator.ground_truth.EventTruth` is built only when the ground
truth is streamed out. Each sink applies the source's clock skew when rendering and assigns the
locator key (``line_key``, ``row_key`` or ``file_key``) after sorting each file by rendered time.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

from carto_simulator.ground_truth import ActorKind, file_key, line_key


@dataclass(slots=True)
class Truth:
    """What the ground truth records about one emitted record (spec 19 outputs)."""

    node: str
    observed_at: datetime
    txn_id: str | None = None
    batch_id: str | None = None
    actor_kind: ActorKind | None = None
    is_error: bool = False


@dataclass(slots=True)
class Emitted:
    """A record after writing: its locator key plus the truth it carries."""

    key: str
    source_id: str
    system_id: str
    truth: Truth


@dataclass(slots=True)
class _Line:
    rendered_at: datetime
    seq: int
    text: str
    truth: Truth


class LogSink:
    """A log-file source: one file per day of the source's own clock, lines in rendered order."""

    def __init__(
        self,
        source_id: str,
        system_id: str,
        directory: str,
        file_name: Callable[[datetime], str],
        file_zone: tzinfo,
        render: Callable[[datetime], str],
        skew: timedelta = timedelta(0),
    ) -> None:
        self.source_id = source_id
        self.system_id = system_id
        self.directory = directory
        self._file_name = file_name
        self._file_zone = file_zone
        self._render = render
        self._skew = skew
        self._files: dict[str, list[_Line]] = {}
        self._names: dict[date, str] = {}
        self._seq = 0
        self.line_counts: dict[str, int] = {}

    def rendered_time(self, observed_at: datetime) -> datetime:
        """The instant as the source's clock shows it (true time plus skew), in UTC."""
        return observed_at + self._skew

    def timestamp(self, observed_at: datetime) -> str:
        """The timestamp string for a record observed at ``observed_at`` (skew applied)."""
        return self._render(self.rendered_time(observed_at))

    def file_name_for(self, rendered_at: datetime) -> str:
        """The file a record rendered at ``rendered_at`` (UTC) lands in, by the source's day."""
        local = rendered_at if self._file_zone is UTC else rendered_at.astimezone(self._file_zone)
        day = local.date()
        name = self._names.get(day)
        if name is None:
            name = self._names[day] = self._file_name(local)
        return name

    def add(self, truth: Truth, text: str) -> None:
        rendered_at = self.rendered_time(truth.observed_at)
        name = self.file_name_for(rendered_at)
        self._seq += 1
        self._files.setdefault(name, []).append(_Line(rendered_at, self._seq, text, truth))

    @property
    def record_count(self) -> int:
        return sum(len(lines) for lines in self._files.values())

    def write(self, out_dir: Path) -> Iterator[Emitted]:
        """Write every file (sorted by rendered time, then insertion) and yield the records."""
        target = out_dir / self.directory
        target.mkdir(parents=True, exist_ok=True)
        for name in sorted(self._files):
            lines = self._files[name]
            lines.sort(key=lambda line: (line.rendered_at, line.seq))
            with (target / name).open("w", encoding="utf-8", newline="\n") as handle:
                for number, line in enumerate(lines, start=1):
                    handle.write(line.text)
                    handle.write("\n")
                    yield Emitted(
                        line_key(self.source_id, name, number),
                        self.source_id,
                        self.system_id,
                        line.truth,
                    )
            self.line_counts[name] = len(lines)

    def relative_paths(self) -> list[str]:
        return [f"{self.directory}/{name}" for name in sorted(self._files)]


@dataclass(slots=True)
class DroppedFile:
    name: str
    content: str
    arrived_at: datetime
    truth: Truth


class FileDropSink:
    """A file-drop source: each file's mtime is set to its true arrival instant (spec 8.1.5)."""

    def __init__(self, source_id: str, system_id: str, directory: str) -> None:
        self.source_id = source_id
        self.system_id = system_id
        self.directory = directory
        self.files: list[DroppedFile] = []

    def add(self, dropped: DroppedFile) -> None:
        self.files.append(dropped)

    def write(self, out_dir: Path) -> Iterator[Emitted]:
        target = out_dir / self.directory
        target.mkdir(parents=True, exist_ok=True)
        for dropped in sorted(self.files, key=lambda item: item.name):
            path = target / dropped.name
            path.write_text(dropped.content, encoding="utf-8", newline="\n")
            stamp = dropped.arrived_at.timestamp()
            os.utime(path, (stamp, stamp))
            yield Emitted(
                file_key(self.source_id, dropped.name),
                self.source_id,
                self.system_id,
                dropped.truth,
            )

    def relative_paths(self) -> list[str]:
        return [
            f"{self.directory}/{item.name}" for item in sorted(self.files, key=lambda f: f.name)
        ]
