from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Settings:
    """Process-level settings. Values come from the environment unless overridden in code (tests)."""

    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./registry.db"))
    # "live" sends real HTTP; "mock" routes every gateway call to a spec-derived mock server.
    gateway_mode: Literal["live", "mock"] = field(
        default_factory=lambda: os.environ.get("GATEWAY_MODE", "live")  # type: ignore[arg-type]
    )
    synthesis_model: str = field(default_factory=lambda: os.environ.get("SYNTHESIS_MODEL", "claude-opus-5"))
    synthesis_max_attempts: int = field(default_factory=lambda: int(os.environ.get("SYNTHESIS_MAX_ATTEMPTS", "3")))
    # Base64 of 32 random bytes. Unset means a fixed development key (never deploy without it).
    vault_master_key: str | None = field(default_factory=lambda: os.environ.get("VAULT_MASTER_KEY"))
    # Where OAuth providers redirect back to; must match the redirect_uri registered with each provider.
    public_base_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000"))
    # Start the background token-refresh loop with the API process.
    refresh_scheduler: bool = field(default_factory=lambda: os.environ.get("REFRESH_SCHEDULER", "0") == "1")
    refresh_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("REFRESH_INTERVAL_SECONDS", "60")))
    # Drift worker: triage open incidents, run repairs, start canaries, promote or abort them.
    drift_worker: bool = field(default_factory=lambda: os.environ.get("DRIFT_WORKER", "0") == "1")
    drift_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("DRIFT_INTERVAL_SECONDS", "300")))
    repair_max_rounds: int = field(default_factory=lambda: int(os.environ.get("REPAIR_MAX_ROUNDS", "2")))
    # Share of an integration's calls routed to a candidate while it is in canary, and how many
    # candidate calls must be observed before the canary is judged.
    canary_fraction: float = field(default_factory=lambda: float(os.environ.get("CANARY_FRACTION", "0.5")))
    canary_min_calls: int = field(default_factory=lambda: int(os.environ.get("CANARY_MIN_CALLS", "5")))


def load_settings() -> Settings:
    return Settings()
