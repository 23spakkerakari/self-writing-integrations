"""Composite links (spec 9.5; plan M2 package D): the temporal-adjacency trigger, the matcher
and the ``composite_rules`` repository.

:class:`CompositeMatcher.run` takes the scored links of a pass and the window and, per ordered
system pair ``(A earlier, B later)``:

1. **Trigger.** The pair is a candidate when no exact or bridge link between the two systems
   scores above ``composite_exact_ceiling``, or the best one covers less than
   ``composite_coverage_floor`` of the later end (scenario C: the HRIS carries the candidate id
   in 60% of its rows), *and* the hourly volumes of the two systems cross-correlate at or above
   ``composite_adjacency_min_correlation`` at some lag from zero to
   ``composite_window_business_days x 24`` hours (:func:`cross_correlation`: Pearson over the
   aligned hourly series, B shifted back by the lag). Of the two orderings of a system pair the
   one with the higher peak is kept; an exact tie keeps both.
2. **Blocking** (:mod:`carto_core.linker.composite_blocking`) and **scoring**
   (:mod:`carto_core.linker.composite_em`): components; ``u`` from random pairs; ``m``, the
   prior and the within-block agreement rates of non-matches from EM over
   ``composite_em_iterations`` rounds started at ``m = 0.9``, the sampled ``u`` and the
   one-to-one prior; pair weights from ``m`` and the sampled ``u`` (what the rule stores and
   M3 re-applies); the threshold on those weights at which the posterior of the fitted
   mixture reaches ``composite_target_precision``.
3. **Output.** One :class:`CompositeRule` per system pair with at least ``min_match`` matched
   pairs (the spec 9.3 hard filter applies to composite links too), persisted through a
   :class:`CompositeRuleRepository` under the id of the link, and one
   :class:`~carto_core.linker.review.ScoredLink` between the ends of the strongest component
   (``link_type`` composite, role transaction, direction A to B, score = estimated precision x
   share of A events matched). The pass hands the links to
   :meth:`~carto_core.linker.review.ReviewService.propose`, which never auto-accepts a composite
   link (spec 9.5). The matched pairs are returned in memory for M3's assembly; M2 persists
   their counts only.

Nothing stored or logged here is a token or a value: field refs, forms, kinds, weights, counts,
lags and system ids only (spec 2.3 invariant 2).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import numpy as np
import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_common.logging import get_logger
from carto_core.db.postgres import ping_engine
from carto_core.linker.composite_blocking import (
    COMPOSITE_FORMS,
    SECONDS_PER_DAY,
    Side,
    block_pairs,
    load_side,
)
from carto_core.linker.composite_em import (
    agreement_matrix,
    choose_threshold,
    components_from,
    discover_components,
    estimate_u,
    fellegi_sunter_em,
    one_to_one_prior,
    pair_weights,
    posteriors,
)
from carto_core.linker.model import (
    CompositeComponent,
    CompositeRule,
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
)
from carto_core.linker.repository import RepositoryError
from carto_core.linker.review import ScoredLink, link_id_for
from carto_core.linker.sentence import evidence_sentence
from carto_core.store.memory import quantile_exact_low
from carto_core.store.model import FieldForm, Window

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from sqlalchemy import Engine

    from carto_core.settings import LinkerSettings
    from carto_core.store.model import EventStore, HourlyVolume

__all__ = [
    "MAX_COMPONENTS",
    "Adjacency",
    "CompositeMatch",
    "CompositeMatcher",
    "CompositeOutcome",
    "CompositeReport",
    "CompositeRuleRepository",
    "InMemoryCompositeRuleRepository",
    "PostgresCompositeRuleRepository",
    "StoredCompositeRule",
    "SystemPairTrigger",
    "adjacencies",
    "component_views",
    "cross_correlation",
    "hourly_series",
    "trigger_pairs",
]

log = get_logger(component="linker.composite")

MAX_COMPONENTS: Final = 32
"""The most components a rule stores: ``EvidenceView.components`` (carto_schema.api) holds at
most this many, strongest first."""
HOURS_PER_DAY: Final = 24
SECONDS_PER_HOUR: Final = 3_600
MIN_SERIES_POINTS: Final = 2
"""A Pearson correlation needs at least two aligned points (and some variance) to exist."""


# -- temporal adjacency (trigger) ----------------------------------------------------------------


def hourly_series(
    volumes: Sequence[HourlyVolume], window: Window
) -> tuple[dict[str, NDArray[np.float64]], int]:
    """Dense per-system hourly counts over the window (zeros where a system saw nothing) and
    the number of hours in the series."""
    start = window.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hours = max(1, math.ceil((window.end - start) / timedelta(hours=1)))
    series: dict[str, NDArray[np.float64]] = {}
    for volume in volumes:
        index = int((volume.hour.astimezone(UTC) - start) / timedelta(hours=1))
        if index < 0 or index >= hours:
            continue
        if volume.system_id not in series:
            series[volume.system_id] = np.zeros(hours, dtype=np.float64)
        series[volume.system_id][index] += float(volume.events)
    return series, hours


def cross_correlation(
    a: NDArray[np.float64], b: NDArray[np.float64], max_lag: int
) -> tuple[int, float]:
    """``(lag, correlation)`` of the lag in ``0..max_lag`` hours at which B's series, shifted
    back by the lag, best correlates (Pearson) with A's. A lag leaving fewer than
    :data:`MIN_SERIES_POINTS` aligned hours or a constant series correlates at zero."""
    best_lag, best = 0, 0.0
    n = min(a.shape[0], b.shape[0])
    for lag in range(max_lag + 1):
        if n - lag < MIN_SERIES_POINTS:
            break
        x = a[: n - lag]
        y = b[lag:n]
        if np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
            continue
        value = float(np.corrcoef(x, y)[0, 1])
        if math.isnan(value):
            continue
        if value > best:
            best_lag, best = lag, value
    return best_lag, best


@dataclass(frozen=True, slots=True)
class Adjacency:
    """The peak hourly cross-correlation of an ordered system pair."""

    system_a: str
    system_b: str
    lag_hours: int
    correlation: float


def adjacencies(
    volumes: Sequence[HourlyVolume], window: Window, *, max_lag_hours: int
) -> list[Adjacency]:
    """Every ordered pair of systems with events in the window and its peak correlation."""
    series, _hours = hourly_series(volumes, window)
    systems = sorted(series)
    result: list[Adjacency] = []
    for system_a in systems:
        for system_b in systems:
            if system_a == system_b:
                continue
            lag, correlation = cross_correlation(series[system_a], series[system_b], max_lag_hours)
            result.append(Adjacency(system_a, system_b, lag, correlation))
    return result


@dataclass(frozen=True, slots=True)
class SystemPairTrigger:
    """Why an ordered system pair is (or is not) a composite candidate."""

    system_a: str
    system_b: str
    best_exact_score: float | None
    """The best exact or bridge link between the two systems in this pass; None when none."""
    later_end_coverage: float | None
    """That link's containment of the later system's end (``c_ba`` for A to B)."""
    lag_hours: int
    correlation: float
    triggered: bool
    reason: str
    """``no_exact_link``, ``low_coverage``, ``exact_link``, ``no_adjacency`` or ``reverse``."""


def _system_of(end: FieldForm) -> str:
    return end.system_id


def _best_links(links: Sequence[ScoredLink]) -> dict[frozenset[str], ScoredLink]:
    best: dict[frozenset[str], ScoredLink] = {}
    for link in links:
        if link.link_type not in (LinkType.EXACT, LinkType.BRIDGE):
            continue
        systems = frozenset({_system_of(link.a), _system_of(link.b)})
        if len(systems) != 2:
            continue
        current = best.get(systems)
        if current is None or link.score > current.score:
            best[systems] = link
    return best


def trigger_pairs(
    links: Sequence[ScoredLink],
    adjacency: Sequence[Adjacency],
    settings: LinkerSettings,
) -> list[SystemPairTrigger]:
    """Spec 9.5 trigger per ordered system pair (module docstring step 1)."""
    best = _best_links(links)
    peaks = {(item.system_a, item.system_b): item for item in adjacency}
    result: list[SystemPairTrigger] = []
    for item in adjacency:
        link = best.get(frozenset({item.system_a, item.system_b}))
        score = link.score if link is not None else None
        coverage: float | None = None
        if link is not None:
            coverage = (
                link.features.c_ba if _system_of(link.b) == item.system_b else link.features.c_ab
            )
        if link is None or score is None or score <= settings.composite_exact_ceiling:
            weak, reason = True, "no_exact_link"
        elif coverage is not None and coverage < settings.composite_coverage_floor:
            weak, reason = True, "low_coverage"
        else:
            weak, reason = False, "exact_link"
        reverse = peaks.get((item.system_b, item.system_a))
        if weak and item.correlation < settings.composite_adjacency_min_correlation:
            weak, reason = False, "no_adjacency"
        elif weak and reverse is not None and reverse.correlation > item.correlation:
            weak, reason = False, "reverse"
        result.append(
            SystemPairTrigger(
                system_a=item.system_a,
                system_b=item.system_b,
                best_exact_score=score,
                later_end_coverage=coverage,
                lag_hours=item.lag_hours,
                correlation=item.correlation,
                triggered=weak,
                reason=reason,
            )
        )
    return result


# -- rules repository ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCompositeRule:
    """One ``composite_rules`` row (spec 7.3), keyed by the id of its link."""

    id: str
    key_link_id: str
    rule: CompositeRule
    updated_at: datetime | None = None


@runtime_checkable
class CompositeRuleRepository(Protocol):
    def get(self, tenant_id: str, link_id: str) -> StoredCompositeRule | None: ...

    def list_rules(self, tenant_id: str) -> list[StoredCompositeRule]:
        """Every rule of the tenant, ordered by id."""
        ...

    def upsert(
        self, tenant_id: str, link_id: str, rule: CompositeRule, now: datetime
    ) -> StoredCompositeRule:
        """Insert or overwrite the rule of a link; returns the stored row."""
        ...

    def ping(self) -> bool: ...


def components_json(rule: CompositeRule) -> list[dict[str, Any]]:
    return [
        {
            "a": {"field_ref": component.a.field_ref, "form": component.a.form},
            "b": {"field_ref": component.b.field_ref, "form": component.b.form},
            "kind": component.kind,
            "m": component.m,
            "u": component.u,
        }
        for component in rule.components
    ]


def components_from_json(document: object) -> tuple[CompositeComponent, ...]:
    if not isinstance(document, list):
        return ()
    components: list[CompositeComponent] = []
    for item in document:
        if not isinstance(item, Mapping):
            continue
        a = item.get("a")
        b = item.get("b")
        if not isinstance(a, Mapping) or not isinstance(b, Mapping):
            continue
        components.append(
            CompositeComponent(
                a=FieldForm(str(a.get("field_ref", "")), str(a.get("form", ""))),
                b=FieldForm(str(b.get("field_ref", "")), str(b.get("form", ""))),
                kind=str(item.get("kind", "")),
                m=float(item.get("m", 0.0)),
                u=float(item.get("u", 0.0)),
            )
        )
    return tuple(components)


class InMemoryCompositeRuleRepository:
    """Reference semantics and test double. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: dict[tuple[str, str], StoredCompositeRule] = {}

    def get(self, tenant_id: str, link_id: str) -> StoredCompositeRule | None:
        with self._lock:
            return self._rules.get((tenant_id, link_id))

    def list_rules(self, tenant_id: str) -> list[StoredCompositeRule]:
        with self._lock:
            rows = [rule for (tenant, _id), rule in self._rules.items() if tenant == tenant_id]
        return sorted(rows, key=lambda row: row.id)

    def upsert(
        self, tenant_id: str, link_id: str, rule: CompositeRule, now: datetime
    ) -> StoredCompositeRule:
        stored = StoredCompositeRule(id=link_id, key_link_id=link_id, rule=rule, updated_at=now)
        with self._lock:
            self._rules[(tenant_id, link_id)] = stored
        return stored

    def ping(self) -> bool:
        return True


_RULE_COLUMNS = (
    "id, key_link_id, components_json, window_seconds, threshold, estimated_precision, updated_at"
)
_SELECT_RULE = sqlalchemy.text(
    f"SELECT {_RULE_COLUMNS} FROM composite_rules WHERE tenant_id = :tenant_id AND id = :id"  # noqa: S608
)
_SELECT_RULES = sqlalchemy.text(
    f"SELECT {_RULE_COLUMNS} FROM composite_rules WHERE tenant_id = :tenant_id ORDER BY id"  # noqa: S608
)
_UPSERT_RULE = sqlalchemy.text(
    "INSERT INTO composite_rules (tenant_id, id, key_link_id, components_json, window_seconds, "  # noqa: S608 - the only interpolation is the constant column list
    "threshold, estimated_precision, created_at, updated_at) "
    "VALUES (:tenant_id, :id, :key_link_id, CAST(:components_json AS JSONB), :window_seconds, "
    ":threshold, :estimated_precision, :now, :now) "
    "ON CONFLICT (tenant_id, id) DO UPDATE SET key_link_id = EXCLUDED.key_link_id, "
    "components_json = EXCLUDED.components_json, window_seconds = EXCLUDED.window_seconds, "
    "threshold = EXCLUDED.threshold, estimated_precision = EXCLUDED.estimated_precision, "
    "updated_at = EXCLUDED.updated_at "
    f"RETURNING {_RULE_COLUMNS}"
)


def _rule_from_row(row: Any) -> StoredCompositeRule:
    document = row.components_json
    if isinstance(document, str | bytes):
        document = json.loads(document)
    return StoredCompositeRule(
        id=row.id,
        key_link_id=row.key_link_id,
        rule=CompositeRule(
            components=components_from_json(document),
            window_seconds=int(row.window_seconds),
            threshold=float(row.threshold),
            estimated_precision=(
                None if row.estimated_precision is None else float(row.estimated_precision)
            ),
        ),
        updated_at=row.updated_at,
    )


class PostgresCompositeRuleRepository:
    """``composite_rules`` (migration 0004)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, tenant_id: str, link_id: str) -> StoredCompositeRule | None:
        try:
            with self._engine.connect() as connection:
                first = connection.execute(
                    _SELECT_RULE, {"tenant_id": tenant_id, "id": link_id}
                ).first()
        except SQLAlchemyError as exc:
            raise RepositoryError(f"rule read failed: {type(exc).__name__}") from exc
        return None if first is None else _rule_from_row(first)

    def list_rules(self, tenant_id: str) -> list[StoredCompositeRule]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_RULES, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise RepositoryError(f"rule list failed: {type(exc).__name__}") from exc
        return [_rule_from_row(row) for row in rows]

    def upsert(
        self, tenant_id: str, link_id: str, rule: CompositeRule, now: datetime
    ) -> StoredCompositeRule:
        parameters = {
            "tenant_id": tenant_id,
            "id": link_id,
            "key_link_id": link_id,
            "components_json": json.dumps(components_json(rule), sort_keys=True),
            "window_seconds": rule.window_seconds,
            "threshold": rule.threshold,
            "estimated_precision": rule.estimated_precision,
            "now": now,
        }
        try:
            with self._engine.begin() as connection:
                first = connection.execute(_UPSERT_RULE, parameters).first()
        except SQLAlchemyError as exc:
            raise RepositoryError(f"rule upsert failed: {type(exc).__name__}") from exc
        if first is None:
            msg = "rule upsert returned no row"
            raise RepositoryError(msg)
        return _rule_from_row(first)

    def ping(self) -> bool:
        return ping_engine(self._engine)


# -- the matcher ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompositeMatch:
    """One matched event pair, kept in memory for M3 (never persisted in M2)."""

    event_id_a: str
    event_id_b: str
    weight: float


@dataclass(frozen=True, slots=True)
class CompositeOutcome:
    """What one triggered system pair produced."""

    trigger: SystemPairTrigger
    events_a: int
    events_b: int
    blocked_pairs: int
    capped: bool
    rule: CompositeRule | None
    link: ScoredLink | None
    matches: tuple[CompositeMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class CompositeReport:
    """Counts of one composite run plus the outcomes, for the pass and the eval report."""

    tenant_id: str
    window: Window
    pairs_considered: int
    pairs_triggered: int
    rules: int
    matched_pairs: int
    capped: int
    outcomes: tuple[CompositeOutcome, ...] = ()

    @property
    def links(self) -> tuple[ScoredLink, ...]:
        return tuple(o.link for o in self.outcomes if o.link is not None)


def _end_view(end: FieldForm) -> dict[str, Any]:
    system_id, _sep, rest = end.field_ref.partition("/")
    template_id, _sep, path = rest.partition("/")
    return {
        "field_id": end.field_ref,
        "system_id": system_id,
        "template_id": template_id,
        "path": path or end.field_ref,
        "form": end.form,
    }


def component_views(rule: CompositeRule) -> list[dict[str, Any]]:
    """``EvidenceView.components`` (carto_schema.api) as JSON-safe dicts, strongest first, with
    exactly the contract's keys so the API can pass them through; the kinds travel beside them
    in the link's ``extra`` and in the ``composite_rules`` row."""
    ordered = sorted(rule.components, key=lambda c: -c.weight)[:MAX_COMPONENTS]
    return [
        {
            "a": _end_view(component.a),
            "b": _end_view(component.b),
            "weight": component.weight,
            "m": component.m,
            "u": component.u,
        }
        for component in ordered
    ]


def _seed(tenant_id: str, system_a: str, system_b: str) -> int:
    digest = hashlib.sha256("\n".join((tenant_id, system_a, system_b)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class CompositeMatcher:
    """See the module docstring. :meth:`hook` is the ``composite_hook`` of
    :class:`~carto_core.linker.run.LinkPass`; :meth:`run` is the same step with its report."""

    def __init__(
        self,
        *,
        store: EventStore,
        settings: LinkerSettings,
        rules: CompositeRuleRepository,
        clock: Callable[[], datetime] | None = None,
        system_names: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._rules = rules
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._system_names = dict(system_names) if system_names else {}
        self._last_report: CompositeReport | None = None

    @property
    def last_report(self) -> CompositeReport | None:
        return self._last_report

    def hook(
        self, *, tenant_id: str, window: Window, links: Sequence[ScoredLink]
    ) -> tuple[ScoredLink, ...]:
        """The pass's ``composite_hook``: returns the composite links for it to propose."""
        return self.run(tenant_id, window, links).links

    def run(self, tenant_id: str, window: Window, links: Sequence[ScoredLink]) -> CompositeReport:
        settings = self._settings
        if not settings.composite_enabled:
            report = CompositeReport(tenant_id, window, 0, 0, 0, 0, 0)
            self._last_report = report
            return report
        max_lag = settings.composite_window_business_days * HOURS_PER_DAY
        volumes = self._store.hourly_volumes(tenant_id, window)
        adjacency = adjacencies(volumes, window, max_lag_hours=max_lag)
        triggers = trigger_pairs(links, adjacency, settings)
        now = self._clock()
        outcomes: list[CompositeOutcome] = []
        sides: dict[str, Side] = {}
        for trigger in triggers:
            if not trigger.triggered:
                continue
            side_a = self._side(sides, tenant_id, trigger.system_a, window)
            side_b = self._side(sides, tenant_id, trigger.system_b, window)
            outcomes.append(self._match(tenant_id, window, trigger, side_a, side_b, now))
        report = CompositeReport(
            tenant_id=tenant_id,
            window=window,
            pairs_considered=len(triggers),
            pairs_triggered=sum(1 for t in triggers if t.triggered),
            rules=sum(1 for o in outcomes if o.rule is not None),
            matched_pairs=sum(len(o.matches) for o in outcomes),
            capped=sum(1 for o in outcomes if o.capped),
            outcomes=tuple(outcomes),
        )
        log.info(
            "composite pass done",
            tenant_id=tenant_id,
            pairs_considered=report.pairs_considered,
            pairs_triggered=report.pairs_triggered,
            rules=report.rules,
            matched_pairs=report.matched_pairs,
            capped=report.capped,
        )
        self._last_report = report
        return report

    def _side(self, cache: dict[str, Side], tenant_id: str, system_id: str, window: Window) -> Side:
        side = cache.get(system_id)
        if side is None:
            side = load_side(
                system_id,
                self._store.events_with_keys(tenant_id, system_id, window, forms=COMPOSITE_FORMS),
            )
            cache[system_id] = side
        return side

    def _match(
        self,
        tenant_id: str,
        window: Window,
        trigger: SystemPairTrigger,
        side_a: Side,
        side_b: Side,
        now: datetime,
    ) -> CompositeOutcome:
        settings = self._settings
        blocked = block_pairs(
            side_a,
            side_b,
            window_business_days=settings.composite_window_business_days,
            max_pairs=settings.composite_max_pairs,
        )
        if blocked.capped:
            log.warning(
                "composite blocking capped",
                tenant_id=tenant_id,
                system_a=trigger.system_a,
                system_b=trigger.system_b,
                pairs=len(blocked.pairs),
            )
        empty = CompositeOutcome(
            trigger, len(side_a), len(side_b), len(blocked.pairs), blocked.capped, None, None
        )
        if not blocked.pairs:
            return empty
        specs = discover_components(side_a, side_b, blocked.pairs)
        if not specs:
            return empty
        rng = np.random.default_rng(_seed(tenant_id, trigger.system_a, trigger.system_b))
        u = estimate_u(specs, side_a, side_b, blocked=frozenset(blocked.pairs), rng=rng)
        matrix = agreement_matrix(specs, side_a, side_b, blocked.pairs)
        em = fellegi_sunter_em(
            matrix,
            u,
            iterations=settings.composite_em_iterations,
            initial_prior=one_to_one_prior(len(side_a), len(side_b), len(blocked.pairs)),
            update_u=True,
        )
        weights = pair_weights(matrix, em.m, u)
        posterior = posteriors(matrix, em.m, em.u, em.prior)
        choice = choose_threshold(weights, posterior, settings.composite_target_precision)
        if choice is None:
            return empty
        selected = np.flatnonzero(weights >= choice.threshold)
        matches = tuple(
            CompositeMatch(
                event_id_a=side_a.events[blocked.pairs[row][0]].event_id,
                event_id_b=side_b.events[blocked.pairs[row][1]].event_id,
                weight=float(weights[row]),
            )
            for row in selected.tolist()
        )
        if len(matches) < settings.min_match:
            log.info(
                "composite rule below the match floor",
                tenant_id=tenant_id,
                system_a=trigger.system_a,
                system_b=trigger.system_b,
                matched=len(matches),
            )
            return empty
        components = tuple(
            sorted(components_from(specs, em.m, u), key=lambda c: -c.weight)[:MAX_COMPONENTS]
        )
        rule = CompositeRule(
            components=components,
            window_seconds=settings.composite_window_business_days * SECONDS_PER_DAY,
            threshold=choice.threshold,
            estimated_precision=choice.estimated_precision,
        )
        link = self._scored_link(
            tenant_id, window, trigger, rule, side_a, side_b, blocked.pairs, selected
        )
        self._rules.upsert(tenant_id, link_id_for(tenant_id, link.a, link.b), rule, now)
        return replace(empty, rule=rule, link=link, matches=matches)

    def _scored_link(
        self,
        tenant_id: str,
        window: Window,
        trigger: SystemPairTrigger,
        rule: CompositeRule,
        side_a: Side,
        side_b: Side,
        pairs: Sequence[tuple[int, int]],
        selected: NDArray[np.int64],
    ) -> ScoredLink:
        strongest = rule.components[0]
        matched_a: set[int] = set()
        matched_b: set[int] = set()
        lags: list[float] = []
        for row in selected.tolist():
            i, j = pairs[row]
            matched_a.add(i)
            matched_b.add(j)
            lags.append(
                (side_b.events[j].observed_at - side_a.events[i].observed_at) / timedelta(seconds=1)
            )
        n_a, n_b = len(side_a), len(side_b)
        q25 = quantile_exact_low(lags, 0.25)
        q75 = quantile_exact_low(lags, 0.75)
        features = PairFeatures(
            n_a=n_a,
            n_b=n_b,
            n_match=len(lags),
            c_ab=len(matched_a) / n_a if n_a else 0.0,
            c_ba=len(matched_b) / n_b if n_b else 0.0,
            lag_median=quantile_exact_low(lags, 0.5),
            lag_iqr=(q75 - q25) if q25 is not None and q75 is not None else None,
            lag_positive_share=(sum(1 for lag in lags if lag > 0) / len(lags)) if lags else None,
            fanout_ab_median=None,
            fanout_ab_p99=None,
            fanout_ba_median=None,
            fanout_ba_p99=None,
            concentration=None,
            chance_ratio=None,
            domain_size=None,
        )
        precision = rule.estimated_precision if rule.estimated_precision is not None else 0.0
        score = min(1.0, max(0.0, precision * features.c_ab))
        extra: dict[str, Any] = {
            "components": component_views(rule),
            "component_kinds": [component.kind for component in rule.components],
            "window_seconds": rule.window_seconds,
            "threshold": rule.threshold,
            "estimated_precision": rule.estimated_precision,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "adjacency_lag_hours": trigger.lag_hours,
            "adjacency_correlation": trigger.correlation,
            "trigger_reason": trigger.reason,
        }
        preview = KeyLink(
            id=link_id_for(tenant_id, strongest.a, strongest.b),
            a=strongest.a,
            b=strongest.b,
            link_type=LinkType.COMPOSITE,
            role=LinkRole.TRANSACTION,
            direction=Direction.A_TO_B,
            score=score,
            status=LinkStatus.PROPOSED,
            features=features,
            affected_volume=features.n_match,
            extra=extra,
        )
        extra["sentence"] = evidence_sentence(preview, system_names=self._system_names)
        return ScoredLink(
            a=strongest.a,
            b=strongest.b,
            link_type=LinkType.COMPOSITE,
            role=LinkRole.TRANSACTION,
            direction=Direction.A_TO_B,
            score=score,
            features=features,
            affected_volume=features.n_match,
            extra=extra,
        )
