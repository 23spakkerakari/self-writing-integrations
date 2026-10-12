"""Request models and helpers shared by the control-plane routers."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.runtime.secrets import DictSecretsProvider, EnvSecretsProvider, SecretNotFound, SecretsProvider


class ConnectionSpec(BaseModel):
    tenant_id: str = "default"
    config: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(
        default_factory=dict, description="secret_ref -> environment variable name, resolved server-side"
    )
    secrets: dict[str, str] = Field(
        default_factory=dict, description="secret_ref -> value. Development only; use secret_env in real deployments"
    )

    def is_empty(self) -> bool:
        return not (self.config or self.secret_env or self.secrets)


class ChainSecrets:
    def __init__(self, *providers: SecretsProvider) -> None:
        self._providers = providers

    def get(self, ref: str) -> str:
        for provider in self._providers:
            try:
                return provider.get(ref)
            except SecretNotFound:
                continue
        raise SecretNotFound(f"secret '{ref}' not provided")


def secrets_for(spec: ConnectionSpec) -> SecretsProvider:
    return ChainSecrets(DictSecretsProvider(spec.secrets), EnvSecretsProvider(spec.secret_env))
