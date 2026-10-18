"""One link pass (plan M2 "How the engine runs" step 4; spec 9.2 to 9.6; ADR 0048, 0053).

:class:`LinkPass.run` takes a tenant through candidates, verification, scoring, proposal,
staleness and families over the window ``[now - window_days, now + 1 day)``:

1. profiles and window sketches from the injected :class:`ProfileSource` (the profiler's
   ``fields`` and ``field_sketches`` tables, package B);
2. :class:`~carto_core.linker.candidates.CandidateGenerator`: bridges, LSH pairs, the cap, and
   the already decided pairs (every pass re-checks accepted links for staleness; a ``full`` pass
   also re-verifies rejected pairs for re-proposal);
3. :func:`~carto_core.linker.verify.verify` (one store join per pair) and
   :class:`~carto_core.linker.score.Scorer`; a pair a hard filter discards is counted and
   dropped unless it is already a decided link, whose evidence is still refreshed so a reviewer
   sees why it decayed;
4. :meth:`ReviewService.propose` upserts proposals, refreshes evidence, applies the re-proposal
   and staleness rules and auto-accept when enabled;
5. :class:`~carto_core.linker.families.FamilyBuilder` rebuilds the entity families;
6. ``composite_hook`` (package D) runs last over the same window, if wired.

Everything a review item shows (the sentence, the top shapes) is computed here from metadata
and stored with the evidence; nothing in it is a token (spec 2.3 invariant 2).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from carto_common.logging import get_logger
from carto_core.linker.candidates import (
    CandidateGenerator,
    ContainmentFn,
    MinHashDecoder,
    default_containment,
    default_minhash_decoder,
)
from carto_core.linker.families import EntityNamer, FamilyBuilder
from carto_core.linker.model import KeyLink, LinkStatus
from carto_core.linker.naming import suggest_entity_name
from carto_core.linker.review import ReviewService, ScoredLink, link_id_for
from carto_core.linker.score import Scorer
from carto_core.linker.sentence import evidence_sentence
from carto_core.linker.verify import DomainSizeFn, default_domain_size, verify
from carto_core.linker.weights import load_weights
from carto_core.store.model import FieldForm, Window

if TYPE_CHECKING:
    from carto_core.audit import AuditRecorder
    from carto_core.linker.repository import EntityRepository, LinkRepository
    from carto_core.profiler.model import FieldProfile, WindowSketch
    from carto_core.settings import LinkerSettings, ProfilerSettings
    from carto_core.store.model import EventStore

__all__ = ["CompositeHook", "LinkPass", "LinkReport", "ProfileSource"]

log = get_logger(component="linker")

CompositeHook = Callable[..., None]
"""Package D's entry point, called as ``hook(tenant_id=..., window=..., links=...)`` with the
links scored in this pass; None until it lands."""


@runtime_checkable
class ProfileSource(Protocol):
    """Where the pass reads the profiler's output (package B's tables)."""

    def profiles(self, tenant_id: str) -> Sequence[FieldProfile]: ...

    def window_sketch(self, tenant_id: str, end: FieldForm) -> WindowSketch | None: ...


@dataclass(frozen=True, slots=True)
class LinkReport:
    """What one pass did; counts only, for the job log and the eval report."""

    tenant_id: str
    window: Window
    full: bool
    weights_version: str
    candidate_ends: int
    candidates_considered: int
    bridge_candidates: int
    truncated: int
    verified: int
    filtered: int
    proposed: int
    updated: int
    reproposed: int
    auto_accepted: int
    stale: int
    recovered: int
    families: int
    families_named: int


class LinkPass:
    """See the module docstring. Every collaborator is injected; the defaults are the
    in-package estimators and the shipped weights."""

    def __init__(
        self,
        *,
        store: EventStore,
        profiles: ProfileSource,
        links: LinkRepository,
        entities: EntityRepository,
        audit: AuditRecorder,
        settings: LinkerSettings,
        profiler_settings: ProfilerSettings,
        clock: Callable[[], datetime] | None = None,
        containment: ContainmentFn = default_containment,
        domain_size: DomainSizeFn = default_domain_size,
        decode_minhash: MinHashDecoder = default_minhash_decoder,
        namer: EntityNamer | None = None,
        system_names: Mapping[str, str] | None = None,
        composite_hook: CompositeHook | None = None,
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._links = links
        self._settings = settings
        self._profiler = profiler_settings
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._domain_size = domain_size
        self._system_names = dict(system_names) if system_names else {}
        self._composite_hook = composite_hook
        self._generator = CandidateGenerator(
            settings, profiler_settings, containment=containment, decode_minhash=decode_minhash
        )
        self._scorer = Scorer(settings, load_weights(settings.weights_file))
        self._review = ReviewService(links, audit, settings, clock=self._clock)
        self._families = FamilyBuilder(
            links, entities, namer=namer if namer is not None else self._default_namer
        )
        self._profile_index: dict[FieldForm, FieldProfile] = {}

    @property
    def review(self) -> ReviewService:
        return self._review

    @property
    def scorer(self) -> Scorer:
        return self._scorer

    def window_for(self, now: datetime) -> Window:
        start = now - timedelta(days=self._profiler.window_days)
        return Window(start=start, end=now + timedelta(days=1))

    def run(self, tenant_id: str, *, full: bool = False, now: datetime | None = None) -> LinkReport:
        at = now if now is not None else self._clock()
        window = self.window_for(at)
        profiles = list(self._profiles.profiles(tenant_id))
        self._profile_index = {profile.end: profile for profile in profiles}
        existing = {
            (link.a, link.b): link
            for link in self._links.list_links(tenant_id)
            if link.status != LinkStatus.PROPOSED
        }
        decided = [
            ends for ends, link in existing.items() if full or link.status != LinkStatus.REJECTED
        ]
        candidates = self._generator.generate(
            tenant_id,
            store=self._store,
            window=window,
            profiles=profiles,
            sketch_of=lambda end: self._profiles.window_sketch(tenant_id, end),
            decided=decided,
        )
        scored: list[ScoredLink] = []
        filtered = 0
        for candidate in candidates.candidates:
            previous = existing.get((candidate.a, candidate.b))
            features = verify(
                self._store,
                tenant_id,
                candidate,
                window,
                domain_size_fn=self._domain_size,
                profiles=self._profile_index,
                cooccurrence=candidates.cooccurrences.get((candidate.a, candidate.b)),
            )
            outcome = self._scorer.score(
                candidate,
                features,
                link_type=previous.link_type if previous is not None else None,
                cooccurrence=candidates.cooccurrences.get((candidate.a, candidate.b)),
            )
            if not outcome.passes_filters and previous is None:
                filtered += 1
                continue
            item = ScoredLink(
                a=candidate.a,
                b=candidate.b,
                link_type=outcome.link_type,
                role=outcome.role,
                direction=outcome.direction,
                score=outcome.score if outcome.passes_filters else 0.0,
                features=features,
                affected_volume=features.n_match,
                extra={},
            )
            scored.append(self._with_sentence(tenant_id, item, outcome.filter_reason))
        proposed = self._review.propose(tenant_id, scored, now=at)
        families = self._families.rebuild(tenant_id, now=at)
        if self._composite_hook is not None:
            self._composite_hook(tenant_id=tenant_id, window=window, links=tuple(scored))
        report = LinkReport(
            tenant_id=tenant_id,
            window=window,
            full=full,
            weights_version=self._scorer.weights.version,
            candidate_ends=sum(1 for profile in profiles if profile.is_candidate),
            candidates_considered=len(candidates.candidates),
            bridge_candidates=candidates.bridge_pairs,
            truncated=candidates.truncated,
            verified=len(candidates.candidates),
            filtered=filtered,
            proposed=proposed.proposed,
            updated=proposed.updated,
            reproposed=proposed.reproposed,
            auto_accepted=proposed.auto_accepted,
            stale=proposed.stale,
            recovered=proposed.recovered,
            families=families.families,
            families_named=families.names_inherited + families.names_suggested,
        )
        log.info(
            "link pass done",
            tenant_id=tenant_id,
            full=full,
            candidates=report.candidates_considered,
            filtered=report.filtered,
            proposed=report.proposed,
            auto_accepted=report.auto_accepted,
            stale=report.stale,
            families=report.families,
        )
        return report

    def _with_sentence(
        self, tenant_id: str, item: ScoredLink, filter_reason: str | None
    ) -> ScoredLink:
        shape_a = self._top_shape(item.a)
        shape_b = self._top_shape(item.b)
        extra: dict[str, Any] = {"shape_a": shape_a, "shape_b": shape_b}
        if filter_reason is not None:
            extra["filter_reason"] = filter_reason
        preview = KeyLink(
            id=link_id_for(tenant_id, item.a, item.b),
            a=item.a,
            b=item.b,
            link_type=item.link_type,
            role=item.role,
            direction=item.direction,
            score=item.score,
            status=LinkStatus.PROPOSED,
            features=item.features,
            affected_volume=item.affected_volume,
            extra=extra,
        )
        extra["sentence"] = evidence_sentence(preview, system_names=self._system_names)
        return ScoredLink(
            a=item.a,
            b=item.b,
            link_type=item.link_type,
            role=item.role,
            direction=item.direction,
            score=item.score,
            features=item.features,
            affected_volume=item.affected_volume,
            extra=extra,
        )

    def _top_shape(self, end: FieldForm) -> str | None:
        profile = self._profile_index.get(end)
        return profile.top_shape if profile is not None else None

    def _default_namer(self, members: Sequence[FieldForm]) -> str | None:
        paths = [end.field_ref.split("/", 2)[-1] for end in members]
        weights = [
            self._profile_index[end].count if end in self._profile_index else 1 for end in members
        ]
        shapes = [shape for end in members if (shape := self._top_shape(end)) is not None]
        return suggest_entity_name(paths, shapes=shapes, weights=weights)
