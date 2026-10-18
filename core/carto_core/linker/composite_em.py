"""Fellegi-Sunter scoring for composite matching (spec 9.5 steps 1 and 3; plan M2 package D).

**Components** (spec 9.5 step 1) are discovered from the blocked pairs, one per kind, or one
per attribute key:

- ``date`` and ``amount``: equal tokens of the ``date`` or ``amount`` form of one A field and
  one B field;
- ``phonetic``: the phonetic tokens (every ``phonetic.<k>``) of one A name field equal those
  of one B name field; the ends are shown with form ``phonetic.0``;
- ``attribute``: the same low-cardinality attribute key carries the same value in both events
  (one component per shared key);
- ``partial_identifier``: a ``digits.<k>`` or ``alnum`` token of one A end equals one of a B
  end (the digits of an order reference reappearing inside a PO number).

For each kind the pair of ends that agrees on the most blocked pairs is the component; a kind
that never agrees has none.

**Weights** (step 3): for component ``k``, ``m_k`` is the agreement probability among true
matches and ``u_k`` among random pairs. ``u`` comes from random sampling of A x B pairs outside
the blocked set (:func:`estimate_u`); ``m`` and the match prior ``p`` come from EM over the
blocked pairs (:func:`fellegi_sunter_em`, conditional independence, ``u`` held fixed). A pair's
weight is the sum over components of ``log2(m/u)`` when it agrees and ``log2((1-m)/(1-u))``
when it does not (:func:`pair_weights`); its posterior match probability is the two-class
mixture at the estimated prior (:func:`posteriors`). The threshold is the lowest weight whose
pairs at or above it have a mean posterior of at least the target precision
(:func:`choose_threshold`); that mean is the rule's ``estimated_precision``.

**Blocking and the mixture.** Among blocked pairs a non-match agrees on the blocking key far
more often than a random pair does (every date-blocked pair shares its date), and with the
random-pair ``u`` held fixed the many near-miss pairs of a large block would be absorbed into
the match class round after round until EM calls everything a match. The matcher therefore
runs EM with ``update_u``: the random-pair ``u`` (raised to the observed agreement rate where
that is higher) is the starting point and the fitted non-match rates describe the blocked
population, which is what the posterior (and so the
precision estimate) must reflect. The pair weights and the rule keep the random-pair ``u``
(spec 9.5), so the stored threshold means the same when M3 re-applies the rule.

**One constraint.** ``m`` is kept at or above ``u`` for every component, in EM and in the
weights: a component never counts against a match. Without it the union of date-blocked and
amount-blocked pairs misleads the mixture: nearly every date-blocked non-match agrees on the
date, so a pair that *disagrees* on the date (an amount-blocked chance pair) would score as
likely a match. Under the constraint such a component is at worst neutral.

Constants here are numerical (an EM starting point, a convergence tolerance, probability
clipping, the sample size of an estimator); every threshold of the spec comes from
:class:`~carto_core.settings.LinkerSettings` (ADR 0055).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from carto_core.linker.composite_blocking import AMOUNT_FORM, DATE_FORM
from carto_core.linker.model import CompositeComponent
from carto_core.store.model import FieldForm

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from carto_core.linker.composite_blocking import Side, SideEvent

__all__ = [
    "ATTRIBUTE_FORM",
    "EM_INITIAL_M",
    "EM_INITIAL_P",
    "EM_TOLERANCE",
    "PHONETIC_END_FORM",
    "PROBABILITY_FLOOR",
    "U_SAMPLE_PAIRS",
    "ComponentSpec",
    "EmResult",
    "ThresholdChoice",
    "agreement_matrix",
    "agrees",
    "choose_threshold",
    "components_from",
    "discover_components",
    "estimate_u",
    "fellegi_sunter_em",
    "one_to_one_prior",
    "pair_weights",
    "posteriors",
]

EM_INITIAL_M: Final = 0.9
"""Where EM starts every ``m`` (plan M2 package D): a true match agrees most of the time."""
EM_INITIAL_P: Final = 0.5
"""Where EM starts the match prior; a flat start that the blocked pairs then pull either way."""
EM_TOLERANCE: Final = 1e-4
"""EM stops early when no ``m`` and the prior moved by more than this between rounds."""
PROBABILITY_FLOOR: Final = 1e-6
"""``m``, ``u`` and the prior are kept inside ``[floor, 1 - floor]`` so no log is infinite."""
U_SAMPLE_PAIRS: Final = 10_000
"""Random A x B pairs drawn to estimate ``u``; a sampling error of about one percent."""
ATTRIBUTE_FORM: Final = "raw"
"""The form an attribute end is shown with: an attribute value travels in clear (spec 8.3)."""
PHONETIC_END_FORM: Final = "phonetic.0"
"""The form a phonetic component's ends are shown with (the truth files name it the same)."""

KIND_DATE: Final = "date"
KIND_AMOUNT: Final = "amount"
KIND_PHONETIC: Final = "phonetic"
KIND_ATTRIBUTE: Final = "attribute"
KIND_PARTIAL: Final = "partial_identifier"


@dataclass(frozen=True, slots=True, order=True)
class ComponentSpec:
    """One component before its weights are known: the kind and the two ends it compares.
    ``key`` is the attribute key of an ``attribute`` component, else None."""

    kind: str
    a: FieldForm
    b: FieldForm
    key: str | None = None


def agrees(spec: ComponentSpec, event_a: SideEvent, event_b: SideEvent) -> bool:
    """Whether one pair agrees on one component (module docstring)."""
    if spec.kind == KIND_DATE:
        token = event_a.dates.get(spec.a.field_ref)
        return token is not None and token == event_b.dates.get(spec.b.field_ref)
    if spec.kind == KIND_AMOUNT:
        token = event_a.amounts.get(spec.a.field_ref)
        return token is not None and token == event_b.amounts.get(spec.b.field_ref)
    if spec.kind == KIND_PHONETIC:
        names = event_a.names.get(spec.a.field_ref)
        return bool(names) and names == event_b.names.get(spec.b.field_ref)
    if spec.kind == KIND_PARTIAL:
        token = event_a.partials.get((spec.a.field_ref, spec.a.form))
        return token is not None and token == event_b.partials.get((spec.b.field_ref, spec.b.form))
    if spec.kind == KIND_ATTRIBUTE and spec.key is not None:
        value = event_a.attributes.get(spec.key)
        return value is not None and value == event_b.attributes.get(spec.key)
    return False


def _attribute_ref(system_id: str, template_id: str, key: str) -> str:
    return f"{system_id}/{template_id}/{key}"


def discover_components(
    a: Side, b: Side, pairs: Sequence[tuple[int, int]]
) -> tuple[ComponentSpec, ...]:
    """The components of a system pair: per kind (per key for attributes) the pair of ends that
    agrees on the most blocked pairs; ties go to the first in :class:`FieldForm` order."""
    counts: dict[ComponentSpec, int] = defaultdict(int)
    for i, j in pairs:
        event_a = a.events[i]
        event_b = b.events[j]
        for ref_a, token in event_a.dates.items():
            for ref_b, other in event_b.dates.items():
                if token == other:
                    counts[
                        ComponentSpec(
                            KIND_DATE, FieldForm(ref_a, DATE_FORM), FieldForm(ref_b, DATE_FORM)
                        )
                    ] += 1
        for ref_a, token in event_a.amounts.items():
            for ref_b, other in event_b.amounts.items():
                if token == other:
                    counts[
                        ComponentSpec(
                            KIND_AMOUNT,
                            FieldForm(ref_a, AMOUNT_FORM),
                            FieldForm(ref_b, AMOUNT_FORM),
                        )
                    ] += 1
        for ref_a, names in event_a.names.items():
            if not names:
                continue
            for ref_b, other_names in event_b.names.items():
                if names == other_names:
                    counts[
                        ComponentSpec(
                            KIND_PHONETIC,
                            FieldForm(ref_a, PHONETIC_END_FORM),
                            FieldForm(ref_b, PHONETIC_END_FORM),
                        )
                    ] += 1
        for end_a, token in event_a.partials.items():
            for end_b, other in event_b.partials.items():
                if token == other:
                    counts[ComponentSpec(KIND_PARTIAL, FieldForm(*end_a), FieldForm(*end_b))] += 1
        for key, value in event_a.attributes.items():
            if event_b.attributes.get(key) == value:
                counts[
                    ComponentSpec(
                        KIND_ATTRIBUTE,
                        FieldForm(
                            _attribute_ref(a.system_id, event_a.template_id, key), ATTRIBUTE_FORM
                        ),
                        FieldForm(
                            _attribute_ref(b.system_id, event_b.template_id, key), ATTRIBUTE_FORM
                        ),
                        key,
                    )
                ] += 1
    best: dict[tuple[str, str | None], tuple[int, ComponentSpec]] = {}
    for spec in sorted(counts):
        group = (spec.kind, spec.key)
        current = best.get(group)
        if current is None or counts[spec] > current[0]:
            best[group] = (counts[spec], spec)
    return tuple(sorted(spec for _count, spec in best.values()))


def agreement_matrix(
    specs: Sequence[ComponentSpec], a: Side, b: Side, pairs: Sequence[tuple[int, int]]
) -> NDArray[np.bool_]:
    """``pairs x components`` agreement, False everywhere when there are no pairs."""
    matrix = np.zeros((len(pairs), len(specs)), dtype=np.bool_)
    for row, (i, j) in enumerate(pairs):
        event_a = a.events[i]
        event_b = b.events[j]
        for column, spec in enumerate(specs):
            if agrees(spec, event_a, event_b):
                matrix[row, column] = True
    return matrix


def _clip(values: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.clip(values, PROBABILITY_FLOOR, 1.0 - PROBABILITY_FLOOR)


def _at_least(m: NDArray[np.float64], u: NDArray[np.float64]) -> NDArray[np.float64]:
    """``m`` kept at or above ``u``: a component never counts against a match (module
    docstring), so agreement weighs at least zero and disagreement at most zero."""
    return _clip(np.maximum(m, u))


def estimate_u(
    specs: Sequence[ComponentSpec],
    a: Side,
    b: Side,
    *,
    blocked: frozenset[tuple[int, int]],
    rng: np.random.Generator,
    sample_pairs: int = U_SAMPLE_PAIRS,
) -> NDArray[np.float64]:
    """``u`` per component from random A x B pairs outside the blocked set, with half a count
    of smoothing so a component that never agrees in the sample keeps a finite weight."""
    total = len(a) * len(b)
    draws = min(sample_pairs, total)
    agree = np.zeros(len(specs), dtype=np.float64)
    drawn = 0
    if draws > 0 and total > 0:
        if total <= draws:
            chosen = np.arange(total)
        else:
            chosen = rng.choice(total, size=draws, replace=False)
        for flat in chosen.tolist():
            i, j = divmod(int(flat), len(b))
            if (i, j) in blocked:
                continue
            drawn += 1
            event_a = a.events[i]
            event_b = b.events[j]
            for column, spec in enumerate(specs):
                if agrees(spec, event_a, event_b):
                    agree[column] += 1.0
    return _clip((agree + 0.5) / (drawn + 1.0))


@dataclass(frozen=True, slots=True)
class EmResult:
    m: NDArray[np.float64]
    u: NDArray[np.float64]
    prior: float
    iterations: int
    converged: bool


def _log_likelihoods(
    matrix: NDArray[np.bool_], m: NDArray[np.float64], u: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Per pair: ``log P(agreements | match)`` and ``log P(agreements | non-match)``."""
    agree = matrix.astype(np.float64)
    log_m = agree @ np.log(m) + (1.0 - agree) @ np.log1p(-m)
    log_u = agree @ np.log(u) + (1.0 - agree) @ np.log1p(-u)
    return log_m, log_u


def _responsibilities(
    matrix: NDArray[np.bool_], m: NDArray[np.float64], u: NDArray[np.float64], prior: float
) -> NDArray[np.float64]:
    log_m, log_u = _log_likelihoods(matrix, m, u)
    logit = (math.log(prior) + log_m) - (math.log1p(-prior) + log_u)
    result: NDArray[np.float64] = 1.0 / (1.0 + np.exp(-logit))
    return result


def one_to_one_prior(events_a: int, events_b: int, pairs: int) -> float:
    """Where EM starts the match prior over blocked pairs: at most one match per event of the
    smaller side, so ``min(n_a, n_b) / pairs``, capped at :data:`EM_INITIAL_P`. A flat start
    lets the many near-miss pairs of a large block outvote the few matches in the first round
    and EM then collapses to "everything matches"; this start does not."""
    if pairs <= 0:
        return EM_INITIAL_P
    return min(EM_INITIAL_P, max(PROBABILITY_FLOOR, min(events_a, events_b) / pairs))


def fellegi_sunter_em(
    matrix: NDArray[np.bool_],
    u: NDArray[np.float64],
    *,
    iterations: int,
    initial_m: float | NDArray[np.float64] = EM_INITIAL_M,
    initial_prior: float = EM_INITIAL_P,
    tolerance: float = EM_TOLERANCE,
    update_u: bool = False,
) -> EmResult:
    """Estimate ``m`` per component and the match prior over the blocked pairs (module
    docstring). ``u`` is held at the sampled value unless ``update_u`` is set, in which case
    it starts at the larger of the sampled value and the observed agreement rate and is
    re-estimated among the pairs too (how :class:`~carto_core.linker.composite.CompositeMatcher`
    runs it). Runs at most ``iterations`` rounds, fewer once every parameter moves by less than
    ``tolerance``."""
    columns = matrix.shape[1]
    if isinstance(initial_m, np.ndarray):
        m = _clip(np.asarray(initial_m, dtype=np.float64).copy())
    else:
        m = _clip(np.full(columns, initial_m, dtype=np.float64))
    u = _clip(np.asarray(u, dtype=np.float64).copy())
    prior = min(max(initial_prior, PROBABILITY_FLOOR), 1.0 - PROBABILITY_FLOOR)
    if matrix.shape[0] == 0 or columns == 0:
        return EmResult(m=m, u=u, prior=prior, iterations=0, converged=False)
    agree = matrix.astype(np.float64)
    if update_u:
        # The observed agreement rate bounds the non-match rate from above (matches only raise
        # it), so it is a safe start for the within-block ``u``; the random-pair value is far
        # below it on a blocking key and would hand every blocked pair a match-sized boost in
        # the first round.
        u = _clip(np.maximum(u, agree.mean(axis=0)))
    m = _at_least(m, u)
    rounds = 0
    converged = False
    while rounds < iterations:
        rounds += 1
        weights = _responsibilities(matrix, m, u, prior)
        mass = float(weights.sum())
        rest = float(matrix.shape[0] - mass)
        new_prior = min(max(mass / matrix.shape[0], PROBABILITY_FLOOR), 1.0 - PROBABILITY_FLOOR)
        new_u = _clip(((1.0 - weights) @ agree) / rest) if update_u and rest > 0 else u
        new_m = _at_least(_clip((weights @ agree) / mass), new_u) if mass > 0 else m
        moved = max(
            float(np.max(np.abs(new_m - m))),
            float(np.max(np.abs(new_u - u))),
            abs(new_prior - prior),
        )
        m, u, prior = new_m, new_u, new_prior
        if moved < tolerance:
            converged = True
            break
    return EmResult(m=m, u=u, prior=prior, iterations=rounds, converged=converged)


def components_from(
    specs: Sequence[ComponentSpec], m: NDArray[np.float64], u: NDArray[np.float64]
) -> tuple[CompositeComponent, ...]:
    return tuple(
        CompositeComponent(a=spec.a, b=spec.b, kind=spec.kind, m=float(m_k), u=float(u_k))
        for spec, m_k, u_k in zip(specs, m.tolist(), u.tolist(), strict=True)
    )


def pair_weights(
    matrix: NDArray[np.bool_], m: NDArray[np.float64], u: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Spec 9.5 pair weight: the sum of log2 likelihood ratios over the components."""
    bounded_u = _clip(u)
    log_m, log_u = _log_likelihoods(matrix, _at_least(m, bounded_u), bounded_u)
    result: NDArray[np.float64] = (log_m - log_u) / math.log(2.0)
    return result


def posteriors(
    matrix: NDArray[np.bool_], m: NDArray[np.float64], u: NDArray[np.float64], prior: float
) -> NDArray[np.float64]:
    """Posterior match probability of every pair under the two-class mixture."""
    bounded = min(max(prior, PROBABILITY_FLOOR), 1.0 - PROBABILITY_FLOOR)
    bounded_u = _clip(u)
    return _responsibilities(matrix, _at_least(m, bounded_u), bounded_u, bounded)


@dataclass(frozen=True, slots=True)
class ThresholdChoice:
    threshold: float
    """Pairs with a weight at or above it match."""
    estimated_precision: float
    """Mean posterior of the matching pairs."""
    matched: int


def choose_threshold(
    weights: NDArray[np.float64], posterior: NDArray[np.float64], target_precision: float
) -> ThresholdChoice | None:
    """The lowest weight whose pairs at or above it have a mean posterior of at least
    ``target_precision``; None when not even the strongest pair reaches it. Pairs are taken in
    weight order and the largest prefix (ending on a weight boundary) whose mean posterior
    meets the target is the match set."""
    if weights.shape[0] == 0:
        return None
    order = np.argsort(-weights, kind="stable")
    sorted_weights = weights[order]
    running = np.cumsum(posterior[order]) / np.arange(1, weights.shape[0] + 1)
    # A cut falls on a weight boundary so equal weights are never split across it.
    boundary = np.ones(weights.shape[0], dtype=np.bool_)
    boundary[:-1] = sorted_weights[:-1] > sorted_weights[1:]
    meeting = np.flatnonzero((running >= target_precision) & boundary)
    if meeting.shape[0] == 0:
        return None
    cut = int(order[int(meeting[-1])])
    threshold = float(weights[cut])
    selected = weights >= threshold
    return ThresholdChoice(
        threshold=threshold,
        estimated_precision=float(posterior[selected].mean()),
        matched=int(selected.sum()),
    )
