"""Assembly of the edge pipeline from settings and a sources file (plan M1 wave 2 seam).

The gateway and the offline analyzer build the same stack: the key manager and its keyring, the
tokenizer, the reveal vault, the PII detector, field statistics, the template store, the
classifier and the :class:`~carto_edge.pipeline.pipeline.EdgePipeline`, plus the audit file.
:func:`build_runtime` does it once, in one order, so the two never diverge on what a key, a
vault or a policy version is.

The two modes differ exactly where ADR 0017 says they do: ``gateway`` streams with the spec's
quarantine rule and persists field statistics under the state directory; ``analyze`` classifies
with complete statistics (quarantine threshold 1, statistics in memory for the run, the
pipeline told not to count pass-2 observations twice). Templates persist in both modes so
``template_id`` is stable across runs of the same state directory (spec 8.2).

Keys are never created here unless the caller asks (``init_local_keys``, the analyzer's
convenience for a pilot without ``carto-ctl``): the gateway refuses to start without
``carto-ctl key init`` (spec 8.4 "generated at install"). Nothing here logs key material or a
record value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from carto_common.crypto import Keyring
from carto_common.logging import get_logger
from carto_edge.audit import EdgeAudit
from carto_edge.config import EdgeSettings, SourcesFile
from carto_edge.connectors.base import ConnectorContext
from carto_edge.keys import ROTATION_FILE, KeyManagementError, KeyManager
from carto_edge.net.ssrf import DefaultNetworkPolicy
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import PipelineResult
from carto_edge.pipeline.pii import PiiDetector, detector_from_settings
from carto_edge.pipeline.pipeline import EdgePipeline
from carto_edge.pipeline.stats import FieldStatsStore
from carto_edge.pipeline.templates import TemplateStore
from carto_edge.pipeline.tokenize import Tokenizer
from carto_edge.reveal import RevealService
from carto_edge.secrets import EdgeSecretResolver
from carto_edge.vault import RevealVault

__all__ = [
    "EdgeRuntime",
    "Mode",
    "build_connector_context",
    "build_reveal_service",
    "build_runtime",
    "ensure_local_keys",
]

Mode = Literal["gateway", "analyze"]

log = get_logger(component="carto_edge.runtime")


@dataclass(slots=True)
class EdgeRuntime:
    """Everything one edge process shares between its receivers, the analyzer and the reveal
    endpoints. ``repr`` names the mode and the key versions only."""

    mode: Mode
    settings: EdgeSettings
    sources: SourcesFile
    keys: KeyManager
    keyring: Keyring
    tokenizer: Tokenizer
    vault: RevealVault
    detector: PiiDetector
    stats: FieldStatsStore
    templates: TemplateStore
    classifier: Classifier
    pipeline: EdgePipeline
    audit: EdgeAudit
    secrets: EdgeSecretResolver | None = None

    def __repr__(self) -> str:
        return f"EdgeRuntime(mode={self.mode!r}, key_versions={self.key_versions!r})"

    @property
    def tenant_id(self) -> str:
        return self.settings.tenant_id

    @property
    def key_versions(self) -> list[int]:
        """Active first (spec 8.4 dual tokenization)."""
        return self.tokenizer.key_versions

    @property
    def policy_version(self) -> str:
        return self.classifier.policy_version

    def write_vault_entries(self, results: Iterable[PipelineResult]) -> int:
        """Store the ``raw`` form values of the given results in the reveal vault (spec 8.4)."""
        entries = [entry for result in results for entry in result.vault_entries]
        if not entries:
            return 0
        return self.vault.put_many(entries)

    def flush(self) -> None:
        """Persist templates and, in gateway mode, field statistics."""
        self.pipeline.flush()
        if self.stats.path is not None:
            self.stats.save()

    def close(self) -> None:
        self.flush()
        self.templates.close()
        self.vault.close()
        self.audit.close()
        if self.secrets is not None:
            self.secrets.close()


def ensure_local_keys(keys: KeyManager, settings: EdgeSettings) -> bool:
    """Create the local KMS master key and version 1 of the tenant key when no key state exists.

    Only the ``local`` provider is created on the fly (a pilot without an install, spec 4.1);
    any other provider must have been initialised with ``carto-ctl key init``. Returns whether
    keys were created.
    """
    if (settings.keys_dir / ROTATION_FILE).is_file():
        return False
    if settings.kms.provider != "local":
        msg = (
            f"no key state in {settings.keys_dir}; run `carto-ctl key init` for the "
            f"{settings.kms.provider} KMS"
        )
        raise KeyManagementError(msg)
    keys.init(create_local_kms=not settings.local_kms_key_file.is_file())
    log.info("keys.created_for_run", keys_dir=str(settings.keys_dir), provider="local")
    return True


def build_runtime(
    settings: EdgeSettings,
    sources: SourcesFile,
    *,
    mode: Mode = "gateway",
    clock: Callable[[], datetime] | None = None,
    detector: PiiDetector | None = None,
    init_local_keys: bool = False,
) -> EdgeRuntime:
    """Build the stack described in the module docstring. Raises
    :class:`~carto_edge.keys.KeyManagementError` when the keys are missing or unusable."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    keys = KeyManager(settings, clock=clock)
    if init_local_keys:
        ensure_local_keys(keys, settings)
    keyring = keys.load_keyring()
    tenant_id = settings.tenant_id
    tokenizer = Tokenizer(keyring, tenant_id)
    vault = RevealVault(settings.vault_db_file, keys.vault_data_key(), tenant_id)
    pii = detector if detector is not None else detector_from_settings(settings.pii)
    analyze = mode == "analyze"
    classify = settings.classify
    if analyze:
        classify = classify.model_copy(update={"quarantine_samples": 1})
    stats = FieldStatsStore.from_settings(classify, path=None if analyze else settings.stats_file)
    if not analyze:
        stats.load()
    templates = TemplateStore(
        settings.templates_dir, flush_interval_seconds=settings.gateway.stats_flush_seconds
    )
    classifier = Classifier(
        classify, sources.field_policies, pii, stats, policy_version=sources.policy_version
    )
    pipeline = EdgePipeline(
        sources=sources,
        templates=templates,
        classifier=classifier,
        detector=pii,
        tokenizer=tokenizer,
        tenant_id=tenant_id,
        retention_days=settings.retention.events_days,
        clock=clock,
        observe_on_process=not analyze,
    )
    audit = EdgeAudit(settings.audit_file)
    runtime = EdgeRuntime(
        mode=mode,
        settings=settings,
        sources=sources,
        keys=keys,
        keyring=keyring,
        tokenizer=tokenizer,
        vault=vault,
        detector=pii,
        stats=stats,
        templates=templates,
        classifier=classifier,
        pipeline=pipeline,
        audit=audit,
    )
    log.info(
        "runtime.built",
        mode=mode,
        tenant_id=tenant_id,
        sources=len(sources.sources),
        systems=len(sources.systems),
        key_versions=runtime.key_versions,
        policy_version=sources.policy_version,
        quarantine_samples=classify.quarantine_samples,
    )
    return runtime


def build_reveal_service(
    runtime: EdgeRuntime, *, clock: Callable[[], datetime] | None = None
) -> RevealService:
    """The service behind ``/internal/reveal`` and ``/internal/tokenize`` (spec 12); disabled
    until core's assertion public key is installed."""
    return RevealService(
        runtime.vault,
        runtime.tokenizer,
        runtime.keys.load_verify_key(),
        runtime.settings.reveal,
        runtime.audit,
        clock=clock,
    )


def build_connector_context(runtime: EdgeRuntime) -> ConnectorContext:
    """Secrets through the edge resolver (ADR 0013) and hosts through the SSRF policy (spec
    14.7) with the sources file's allow-list. The resolver is owned by the runtime and closed
    with it."""
    if runtime.secrets is None:
        runtime.secrets = EdgeSecretResolver(runtime.settings, runtime.keys.kms)
    return ConnectorContext(
        secrets=runtime.secrets,
        network=DefaultNetworkPolicy(runtime.sources.network.allowed_source_cidrs),
        tenant_id=runtime.tenant_id,
    )
