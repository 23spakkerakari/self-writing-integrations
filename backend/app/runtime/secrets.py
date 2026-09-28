"""Secret resolution. Milestone one resolves from the process environment; the OAuth broker
and vault in milestone two replace this with per-tenant encrypted storage. The gateway is the
only component that ever sees a resolved secret."""
from __future__ import annotations

import os
from typing import Protocol


class SecretNotFound(KeyError):
    pass


class SecretsProvider(Protocol):
    def get(self, ref: str) -> str: ...


class DictSecretsProvider:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, ref: str) -> str:
        try:
            return self._values[ref]
        except KeyError as exc:
            raise SecretNotFound(f"secret '{ref}' is not set on this connection") from exc


class EnvSecretsProvider:
    """Maps a secret ref to an environment variable name, e.g. {'api_key': 'BAMBOOHR_API_KEY'}."""

    def __init__(self, env_names: dict[str, str]) -> None:
        self._env_names = dict(env_names)

    def get(self, ref: str) -> str:
        env_name = self._env_names.get(ref)
        if env_name is None:
            raise SecretNotFound(f"secret '{ref}' has no environment variable mapped")
        value = os.environ.get(env_name)
        if value is None:
            raise SecretNotFound(f"environment variable '{env_name}' for secret '{ref}' is not set")
        return value
