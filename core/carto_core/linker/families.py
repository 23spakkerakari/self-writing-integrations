"""Entity families: connected components over the links that may merge records (spec 9.4,
9.6; ADR 0053).

A family is a connected component of ``(field_ref, form)`` ends under the links whose
:attr:`KeyLink.is_usable_for_families` holds: accepted or auto-accepted transaction links and
bridges. Association links, batch links, proposals, rejections and stale links never join two
ends, so a rejected pair is never inside one family through its own link (the property test in
``test_core_linker_families.py``); it can still share a family through other accepted links,
which is what transitivity through bridges means.

Entity ids are content-addressed: ``ent_`` plus the first 16 hex digits of the SHA-256 of the
sorted member list, so a family that did not change keeps its id and the reviewer's name. When
a family grows (its members are a superset of a previous entity's), the previous entity's
confirmed name and description carry over; the previous entities are matched by member
overlap, largest first, each used once. A family with no inherited name gets a heuristic
suggestion (:mod:`carto_core.linker.naming`) that the reviewer confirms.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from carto_core.linker.repository import Entity

if TYPE_CHECKING:
    from carto_core.linker.model import KeyLink
    from carto_core.linker.repository import EntityRepository, LinkRepository
    from carto_core.store.model import FieldForm

__all__ = [
    "EntityNamer",
    "Family",
    "FamilyBuilder",
    "FamilyReport",
    "build_families",
    "entity_id_for",
    "inherit_names",
]

EntityNamer = Callable[[Sequence["FieldForm"]], str | None]
"""Suggests a name for a family from its member ends; None when nothing names it."""


@dataclass(frozen=True, slots=True)
class Family:
    entity_id: str
    members: tuple[FieldForm, ...]
    """Sorted."""
    link_ids: tuple[str, ...]
    """The usable links inside the family, sorted."""


@dataclass(frozen=True, slots=True)
class FamilyReport:
    families: int
    links_assigned: int
    names_inherited: int
    names_suggested: int


def entity_id_for(members: Iterable[FieldForm]) -> str:
    ordered = sorted(members)
    digest = hashlib.sha256(
        "\n".join(f"{end.field_ref}|{end.form}" for end in ordered).encode("utf-8")
    ).hexdigest()
    return f"ent_{digest[:16]}"


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[FieldForm, FieldForm] = {}

    def find(self, end: FieldForm) -> FieldForm:
        root = self._parent.setdefault(end, end)
        while root != self._parent[root]:
            root = self._parent[root]
        while self._parent[end] != root:
            self._parent[end], end = root, self._parent[end]
        return root

    def union(self, a: FieldForm, b: FieldForm) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        if root_b < root_a:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a


def build_families(links: Sequence[KeyLink]) -> list[Family]:
    """Connected components over the usable links, ordered by entity id."""
    usable = [link for link in links if link.is_usable_for_families]
    if not usable:
        return []
    components = _UnionFind()
    for link in usable:
        components.union(link.a, link.b)
    members: dict[FieldForm, set[FieldForm]] = {}
    link_ids: dict[FieldForm, set[str]] = {}
    for link in usable:
        root = components.find(link.a)
        members.setdefault(root, set()).update((link.a, link.b))
        link_ids.setdefault(root, set()).add(link.id)
    families = [
        Family(
            entity_id=entity_id_for(ends),
            members=tuple(sorted(ends)),
            link_ids=tuple(sorted(link_ids[root])),
        )
        for root, ends in members.items()
    ]
    families.sort(key=lambda family: family.entity_id)
    return families


def inherit_names(
    families: Sequence[Family], previous: Sequence[Entity], namer: EntityNamer | None
) -> tuple[list[Entity], int, int]:
    """Entities for ``families``: a previous entity's name carries over to the family whose
    members contain all of its members (largest overlap first, each previous entity once);
    other families get the namer's suggestion. Returns ``(entities, inherited, suggested)``."""
    candidates: list[tuple[int, str, str]] = []
    member_sets = {family.entity_id: set(family.members) for family in families}
    for entity in previous:
        old = set(entity.members)
        for family in families:
            overlap = len(old & member_sets[family.entity_id])
            if overlap and old <= member_sets[family.entity_id]:
                candidates.append((-overlap, entity.id, family.entity_id))
    candidates.sort()
    inherited_from: dict[str, Entity] = {}
    used_previous: set[str] = set()
    by_id = {entity.id: entity for entity in previous}
    for _negative_overlap, previous_id, family_id in candidates:
        if previous_id in used_previous or family_id in inherited_from:
            continue
        used_previous.add(previous_id)
        inherited_from[family_id] = by_id[previous_id]
    entities: list[Entity] = []
    inherited = suggested = 0
    for family in families:
        source = inherited_from.get(family.entity_id)
        if source is not None and (source.name or source.suggested_name):
            inherited += 1
            entities.append(
                Entity(
                    id=family.entity_id,
                    members=family.members,
                    name=source.name,
                    suggested_name=source.suggested_name,
                    name_confirmed=source.name_confirmed,
                    description=source.description,
                )
            )
            continue
        suggestion = namer(family.members) if namer is not None else None
        if suggestion:
            suggested += 1
        entities.append(
            Entity(
                id=family.entity_id,
                members=family.members,
                suggested_name=suggestion,
                description=source.description if source is not None else None,
            )
        )
    return entities, inherited, suggested


class FamilyBuilder:
    """Rebuilds a tenant's entities from its links and writes them through the repositories."""

    __slots__ = ("_entities", "_links", "_namer")

    def __init__(
        self,
        links: LinkRepository,
        entities: EntityRepository,
        *,
        namer: EntityNamer | None = None,
    ) -> None:
        self._links = links
        self._entities = entities
        self._namer = namer

    def rebuild(self, tenant_id: str, *, now: datetime) -> FamilyReport:
        links = self._links.list_links(tenant_id)
        families = build_families(links)
        previous = self._entities.list_entities(tenant_id)
        entities, inherited, suggested = inherit_names(families, previous, self._namer)
        self._entities.replace_all(tenant_id, entities, now)
        assignments: dict[str, str | None] = {link.id: None for link in links}
        for family in families:
            for link_id in family.link_ids:
                assignments[link_id] = family.entity_id
        assigned = self._links.assign_entities(tenant_id, assignments, now)
        return FamilyReport(
            families=len(families),
            links_assigned=assigned,
            names_inherited=inherited,
            names_suggested=suggested,
        )
