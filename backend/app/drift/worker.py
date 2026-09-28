"""Drift worker: the autonomous part of the loop.

One tick triages every open incident and runs the repair pipeline for the repairable ones,
starts a canary for every approved change request, and judges running canaries: promote on
pass, abort on fail, wait on insufficient data. Approval itself stays with a human unless the
integration's policy says otherwise, so the worker never publishes something nobody agreed to.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from pydantic import BaseModel, Field

from app.drift.changes import ChangeError, ChangeRequests
from app.drift.models import DriftIncident
from app.drift.monitor import DriftMonitor
from app.drift.repair import RepairError, RepairPipeline, World

log = logging.getLogger(__name__)

WorldFactory = Callable[[DriftIncident], World | None]


class WorkerTick(BaseModel):
    triaged: list[int] = Field(default_factory=list)
    repaired: list[int] = Field(default_factory=list)
    canaries_started: list[int] = Field(default_factory=list)
    promoted: list[int] = Field(default_factory=list)
    aborted: list[int] = Field(default_factory=list)
    waiting: list[int] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class DriftWorker:
    def __init__(
        self,
        monitor: DriftMonitor,
        changes: ChangeRequests,
        pipeline: RepairPipeline,
        world_factory: WorldFactory,
        canary_fraction: float = 0.5,
        interval_seconds: int = 300,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.monitor = monitor
        self.changes = changes
        self.pipeline = pipeline
        self.world_factory = world_factory
        self.canary_fraction = canary_fraction
        self.interval = interval_seconds
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> WorkerTick:
        result = WorkerTick()
        candidates = self.monitor.list(status="open") + [
            i for i in self.monitor.list(status="triaged") if i.repairable and i.change_request_id is None
        ]
        for incident in candidates:
            try:
                if incident.drift_class is None:
                    incident, triage = self.pipeline.triage(incident.id)
                    result.triaged.append(incident.id)
                    if not triage.repairable:
                        continue
                world = self.world_factory(incident)
                if world is None:
                    result.errors[f"incident:{incident.id}"] = "no connection available to verify a repair against"
                    continue
                outcome = self.pipeline.run(incident.id, world)
                if outcome.error:
                    result.errors[f"incident:{incident.id}"] = outcome.error
                elif outcome.change_request is not None:
                    result.repaired.append(incident.id)
            except (RepairError, ChangeError) as exc:
                result.errors[f"incident:{incident.id}"] = str(exc)
            except Exception as exc:  # a crashing repair must not stop the worker
                log.exception("repair of incident %s crashed", incident.id)
                result.errors[f"incident:{incident.id}"] = f"{type(exc).__name__}: {exc}"

        for change in self.changes.list(status="approved"):
            try:
                self.changes.start_canary(change.id, self.canary_fraction)
                result.canaries_started.append(change.id)
            except ChangeError as exc:
                result.errors[f"change:{change.id}"] = str(exc)

        for change in self.changes.list(status="canary"):
            try:
                report = self.changes.canary_report(change.id)
                if report.verdict == "pass":
                    self.changes.promote(change.id, actor="drift-worker")
                    result.promoted.append(change.id)
                elif report.verdict == "fail":
                    self.changes.abort_canary(change.id, actor="drift-worker", note=report.reason)
                    result.aborted.append(change.id)
                else:
                    result.waiting.append(change.id)
            except ChangeError as exc:
                result.errors[f"change:{change.id}"] = str(exc)
        return result

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let the loop die
                log.exception("drift tick crashed")
            self._sleep(self.interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="drift-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
