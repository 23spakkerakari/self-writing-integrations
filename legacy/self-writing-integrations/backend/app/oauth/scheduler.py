"""Background token refresh. One tick refreshes every active connection whose access token is
inside its leeway window. Connections whose grant is gone are flipped to needs_reconsent by the
broker (with a notification); transient failures are retried next tick."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from pydantic import BaseModel, Field

from app.oauth.broker import OAuthBroker, OAuthError, ReconsentRequired

log = logging.getLogger(__name__)


class TickResult(BaseModel):
    checked: int = 0
    refreshed: list[str] = Field(default_factory=list)
    reconsent: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class RefreshScheduler:
    def __init__(self, broker: OAuthBroker, interval_seconds: int = 60, sleep: Callable[[float], None] = time.sleep) -> None:
        self.broker = broker
        self.interval = interval_seconds
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> TickResult:
        result = TickResult()
        for conn in self.broker.due_for_refresh():
            result.checked += 1
            try:
                self.broker.refresh(conn.id, reason="scheduled")
                result.refreshed.append(conn.id)
            except ReconsentRequired as exc:
                result.reconsent.append(conn.id)
                log.warning("connection %s needs re-consent: %s", conn.id, exc)
            except OAuthError as exc:
                result.errors[conn.id] = str(exc)
                log.error("refresh of %s failed: %s", conn.id, exc)
        return result

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let the loop die
                log.exception("refresh tick crashed")
            self._sleep(self.interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="oauth-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
