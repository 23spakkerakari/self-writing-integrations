"""Semantic matching: which canonical field does a raw field carry?

This is the deterministic half of the proposer. A field name is split into words ('emp_id',
'supervisorEId', 'worker_number'), abbreviations are expanded, and the phrase is scored against
the names each canonical field goes by. The values seen in the samples then raise or lower the
score: an identifier should be unique, an email should look like one, a date should parse.

Every result is a Suggestion with a confidence and a rationale a reviewer can read. Nothing here
is trusted on its own: suggestions are stored as proposals and a person confirms them.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from app.discovery.cluster import looks_like_id, singular
from app.discovery.infer import parses_as_date
from app.discovery.models import Alternative, FieldStat, Suggestion

MIN_CONFIDENCE = 0.5
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_EXPAND = {
    "emp": "employee", "empl": "employee", "dept": "department", "mgr": "manager", "num": "number", "no": "number",
    "nbr": "number", "nr": "number", "tel": "phone", "telephone": "phone", "cell": "mobile", "fname": "first name",
    "lname": "last name", "eid": "employee id", "uid": "user id", "doj": "joining date", "mail": "email",
    "pos": "position", "org": "organization", "loc": "location", "addr": "address", "desc": "description",
}

Synonyms = dict[str, list[tuple[str, float]]]

_SYNONYMS: dict[str, Synonyms] = {
    "Employee": {
        "source_id": [
            ("id", 0.95), ("employee id", 0.95), ("employee number", 0.92), ("employee uuid", 0.92), ("worker id", 0.92),
            ("worker number", 0.92), ("person id", 0.9), ("staff id", 0.9), ("staff number", 0.9),
            ("personnel number", 0.9), ("uuid", 0.9), ("guid", 0.85), ("user id", 0.85), ("member id", 0.8),
        ],
        "first_name": [("first name", 1.0), ("given name", 0.95), ("forename", 0.95), ("first", 0.7)],
        "last_name": [("last name", 1.0), ("family name", 0.95), ("surname", 0.95), ("last", 0.7)],
        "display_name": [
            ("display name", 1.0), ("full name", 0.95), ("employee name", 0.9), ("formatted name", 0.85), ("name", 0.85),
            ("preferred name", 0.55),
        ],
        "work_email": [
            ("work email", 1.0), ("business email", 0.95), ("company email", 0.95), ("corporate email", 0.95),
            ("office email", 0.9), ("email", 0.85), ("email address", 0.85),
        ],
        "personal_email": [("personal email", 1.0), ("home email", 0.95), ("private email", 0.95)],
        "job_title": [
            ("job title", 1.0), ("position title", 0.9), ("position", 0.85), ("designation", 0.85), ("title", 0.8),
            ("job", 0.8), ("job name", 0.8), ("role", 0.7),
        ],
        "department": [("department", 1.0), ("department name", 0.95), ("team", 0.75), ("organization unit", 0.7)],
        "division": [("division", 1.0), ("business unit", 0.85)],
        "location": [
            ("location", 1.0), ("work location", 0.95), ("office location", 0.95), ("office", 0.8), ("site", 0.8), ("city", 0.6),
        ],
        "manager_source_id": [
            ("manager id", 1.0), ("supervisor id", 1.0), ("line manager id", 1.0), ("manager employee id", 1.0),
            ("manager uuid", 1.0), ("manager worker number", 0.95), ("manager employee number", 0.95),
            ("reports to id", 0.95), ("manager number", 0.9),
        ],
        "manager_display_name": [
            ("manager name", 1.0), ("supervisor name", 1.0), ("line manager", 0.9), ("manager", 0.85), ("supervisor", 0.85),
            ("reports to", 0.85),
        ],
        "hire_date": [
            ("hire date", 1.0), ("date hired", 0.95), ("hired on", 0.95), ("employment start date", 0.95),
            ("start date", 0.9), ("date of joining", 0.9), ("joining date", 0.9), ("original hire date", 0.9),
            ("joined on", 0.85), ("joined", 0.8), ("started on", 0.8),
        ],
        "termination_date": [
            ("termination date", 1.0), ("terminated on", 0.95), ("employment end date", 0.95), ("leaving date", 0.9),
            ("date of leaving", 0.9), ("exit date", 0.9), ("end date", 0.8), ("last day", 0.8),
        ],
        "employment_status": [
            ("employment status", 1.0), ("employee status", 1.0), ("status", 0.85), ("state", 0.7), ("active", 0.7),
            ("is active", 0.7), ("terminated", 0.7),
        ],
        "work_phone": [
            ("work phone", 1.0), ("work phone number", 1.0), ("office phone", 0.95), ("business phone", 0.95), ("phone", 0.75),
            ("phone number", 0.75),
        ],
        "mobile_phone": [("mobile phone", 1.0), ("mobile number", 0.95), ("mobile", 0.9)],
    },
    "Department": {
        "source_id": [("id", 0.95), ("department id", 0.95), ("department code", 0.9), ("uuid", 0.9), ("code", 0.85)],
        "name": [("department name", 1.0), ("name", 0.95), ("title", 0.7), ("label", 0.6)],
        "parent_source_id": [
            ("parent id", 1.0), ("parent department id", 1.0), ("parent code", 0.95), ("parent department", 0.85), ("parent", 0.8),
        ],
    },
}

_MANAGER_WORDS = {"manager", "supervisor", "boss", "lead"}
_OTHER_PERSON_WORDS = {"emergency", "contact", "spouse", "partner", "kin", "previous", "former", "old", "referrer"}
_OBJECT_HINTS = {
    "Department": {"department", "departments", "dept", "depts", "team", "teams", "division", "divisions", "organization", "units"},
    "Employee": {
        "employee", "employees", "people", "person", "persons", "staff", "worker", "workers", "hire", "hires", "learner",
        "learners", "user", "users", "member", "members", "directory",
    },
}
_STATUS_VALUES = {
    "active": {"active", "enabled", "current", "employed", "started", "working", "hired", "onboarded", "full time", "part time"},
    "terminated": {
        "inactive", "terminated", "former", "left", "offboarded", "withdrawn", "resigned", "dismissed", "deactivated",
        "disabled", "ended",
    },
    "on_leave": {"leave", "on leave", "onleave", "loa", "suspended", "sabbatical", "furlough", "furloughed"},
}
_DATE_TARGETS = {"hire_date", "termination_date"}
_EMAIL_TARGETS = {"work_email", "personal_email"}
_PHONE_TARGETS = {"work_phone", "mobile_phone"}
_ID_TARGETS = {"source_id", "manager_source_id", "parent_source_id"}


def words(name: str) -> tuple[str, ...]:
    """'supervisorEId' -> ('supervisor', 'id'); 'mgr_emp_id' -> ('manager', 'employee', 'id')."""
    tokens: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", name):
        tokens.extend(m.group(0).lower() for m in _CAMEL.finditer(chunk))
    expanded: list[str] = []
    for token in tokens:
        expanded.extend(_EXPAND.get(token, token).split())
    return tuple(t for t in expanded if len(t) > 1)


def source_path(stat_path: str) -> str:
    """A FieldStat path as a mapping source: the first element stands for a list ('jobs.0.title')."""
    return stat_path.replace("[]", ".0")


@dataclass
class _Candidate:
    target: str
    stat: FieldStat
    score: float
    reasons: list[str] = field(default_factory=list)


def _phrase_score(tokens: tuple[str, ...], synonym: tuple[str, ...]) -> float:
    if not tokens:
        return 0.0
    if tokens == synonym:
        return 1.0
    have, want = set(tokens), set(synonym)
    if have == want:
        return 0.95
    if want < have:
        return max(0.5, 0.9 - 0.15 * (len(have) - len(want)))
    if have < want:
        return 0.6
    ratio = difflib.SequenceMatcher(None, " ".join(tokens), " ".join(synonym)).ratio()
    return 0.75 * ratio if ratio >= 0.86 else 0.0


def _name_score(stat_path: str, target: str, synonyms: list[tuple[str, float]]) -> tuple[float, str]:
    """Best score of a field name against one canonical field's names, and the name that matched."""
    parts = stat_path.split(".")
    leaf = words(parts[-1])
    phrases = [(leaf, 1.0 if len(parts) == 1 else 0.85)]
    if len(parts) > 1:
        parent = tuple(singular(w) for w in words(parts[-2]))
        phrases.append((parent + leaf, 0.9))
    best, matched = 0.0, ""
    for tokens, weight in phrases:
        about_someone_else = set(tokens) & (_OTHER_PERSON_WORDS | (_MANAGER_WORDS if not target.startswith("manager_") else set()))
        if target != "parent_source_id" and "parent" in tokens:
            about_someone_else = about_someone_else | {"parent"}
        for phrase, strength in synonyms:
            score = _phrase_score(tokens, tuple(phrase.split())) * strength * weight
            if about_someone_else:
                score *= 0.25
            if score > best:
                best, matched = score, phrase
    return best, matched


def _evidence(target: str, stat: FieldStat) -> tuple[float, str]:
    """What the sampled values say: a multiplier on the name score and the reason for it."""
    kinds, examples = stat.types, stat.examples
    if kinds and set(kinds) <= {"object", "array"}:
        return 0.0, "the value is a structure, not a single field"
    strings = [e for e in examples if isinstance(e, str)]
    if target in _EMAIL_TARGETS:
        if stat.format == "email":
            return 1.05, "values are email addresses"
        return (0.4, "values are not email addresses") if examples else (1.0, "")
    if target in _DATE_TARGETS:
        if examples and all(parses_as_date(e) for e in examples):
            return 1.05, "values parse as dates"
        return (0.4, "values do not parse as dates") if examples else (1.0, "")
    if target in _PHONE_TARGETS:
        if strings and all(sum(c.isdigit() for c in s) >= 6 for s in strings):
            return 1.05, "values look like phone numbers"
        return (0.6, "values do not look like phone numbers") if examples else (1.0, "")
    if target in _ID_TARGETS:
        if not set(kinds) <= {"string", "integer"}:
            return 0.3, "identifiers are strings or integers"
        if strings and any(" " in s.strip() for s in strings):
            return 0.4, "values contain spaces, which identifiers rarely do"
        if target == "source_id" and stat.present >= 2:
            if stat.distinct == stat.present:
                return 1.05, f"values are unique across {stat.present} records"
            return 0.5, "values repeat, so the field does not identify a record"
        return 1.0, ""
    if target == "employment_status":
        if set(kinds) <= {"boolean", "string"}:
            return 1.0, ""
        return 0.4, "a status is a word or a flag"
    if kinds and kinds != ["string"]:
        return 0.3, "the canonical field is text"
    if target == "manager_display_name" and strings and all(looks_like_id(s) for s in strings):
        return 0.4, "values look like identifiers, not names"
    return 1.0, ""


def _transform_for(target: str, stat: FieldStat) -> tuple[str | None, dict[str, Any], float, str]:
    """(transform, args, confidence multiplier, note) for carrying a raw value into a canonical field."""
    if target in _DATE_TARGETS:
        return "to_date", {}, 1.0, ""
    if target in ("manager_source_id", "parent_source_id") and "integer" in stat.types:
        return "to_str", {}, 1.0, ""
    if target != "employment_status":
        return None, {}, 1.0, ""
    name = set(words(stat.path.split(".")[-1]))
    if stat.types == ["boolean"]:
        negative = bool(name & {"terminated", "inactive", "deleted", "archived", "disabled", "left"})
        mapping = {"true": "terminated", "false": "active"} if negative else {"true": "active", "false": "terminated"}
        return "enum_map", {"map": mapping, "default": "unknown"}, 1.0, "a flag: true means " + mapping["true"]
    values = stat.enum or [e for e in stat.examples if isinstance(e, str)]
    mapping, unknown = {}, []
    for value in values:
        phrase = " ".join(words(str(value))) or str(value).lower()
        meaning = next((canon for canon, names in _STATUS_VALUES.items() if phrase in names), None)
        if meaning is None:
            unknown.append(str(value))
        else:
            mapping[str(value)] = meaning
    note = ""
    factor = 1.0
    if unknown:
        note = f"no obvious meaning for {unknown}; they map to 'unknown' until someone decides"
        factor = 0.75
    if not mapping:
        factor = 0.6
    return "enum_map", {"map": mapping, "default": "unknown"}, factor, note


def _candidates(fields: list[FieldStat], canonical_object: str) -> list[_Candidate]:
    out: list[_Candidate] = []
    for target, synonyms in _SYNONYMS[canonical_object].items():
        for stat in fields:
            score, matched = _name_score(stat.path, target, synonyms)
            if score <= 0:
                continue
            multiplier, why = _evidence(target, stat)
            score *= multiplier * (0.75 + 0.25 * stat.confidence)
            if score < MIN_CONFIDENCE:
                continue
            spoken = " ".join(words(stat.path.split(".")[-1]))
            reasons = [f"'{stat.path}' reads as '{spoken}', a name for {target} ('{matched}')"]
            if why:
                reasons.append(why)
            if stat.total and stat.total < 3:
                reasons.append(f"only {stat.total} record(s) seen")
            out.append(_Candidate(target, stat, min(score, 0.99), reasons))
    return out


def choose_object(fields: list[FieldStat], endpoint_path: str = "") -> str | None:
    """Which canonical object the records look like, or None when they look like neither."""
    hinted = {obj for obj, hints in _OBJECT_HINTS.items() if set(words(endpoint_path)) & hints}
    targets = {
        obj: {c.target for c in _candidates(fields, obj) if c.target != "source_id"} for obj in _SYNONYMS
    }
    if len(hinted) == 1:
        (only,) = hinted
        if targets[only]:
            return only
    if len(targets["Employee"]) >= 2:
        return "Employee"
    if "name" in targets["Department"]:
        return "Department"
    return "Employee" if targets["Employee"] else None


def record_affinity(keys: list[str]) -> float:
    """How many of these keys read as canonical fields; used to find the record list in a body."""
    stats = [FieldStat(path=k, types=[], present=0, total=0, confidence=1.0) for k in keys]
    matched: set[str] = set()
    for synonyms_by_target in _SYNONYMS.values():
        for target, synonyms in synonyms_by_target.items():
            for stat in stats:
                if _name_score(stat.path, target, synonyms)[0] >= 0.7:
                    matched.add(stat.path)
    return float(len(matched))


def propose_mappings(fields: list[FieldStat], canonical_object: str) -> list[Suggestion]:
    """Read direction: one Suggestion per canonical field that some raw field appears to carry.
    Each raw field serves at most one canonical field; the runners-up are kept as alternatives."""
    candidates = sorted(_candidates(fields, canonical_object), key=lambda c: (-c.score, c.stat.path.count("."), c.stat.path))
    taken_targets: dict[str, _Candidate] = {}
    taken_sources: set[str] = set()
    for c in candidates:
        if c.target in taken_targets or c.stat.path in taken_sources:
            continue
        taken_targets[c.target] = c
        taken_sources.add(c.stat.path)

    suggestions: list[Suggestion] = []
    for target, chosen in taken_targets.items():
        transform, args, factor, note = _transform_for(target, chosen.stat)
        reasons = chosen.reasons + ([note] if note else [])
        alternatives = [
            Alternative(source_path=source_path(c.stat.path), confidence=round(c.score, 2))
            for c in candidates
            if c.target == target and c.stat.path != chosen.stat.path
        ][:3]
        suggestions.append(
            Suggestion(
                canonical_object=canonical_object,
                target=target,
                source_path=source_path(chosen.stat.path),
                transform=transform,
                args=args,
                confidence=round(chosen.score * factor, 2),
                rationale="; ".join(reasons),
                alternatives=alternatives,
            )
        )

    by_target = {s.target: s for s in suggestions}
    if canonical_object == "Employee" and "display_name" not in by_target and {"first_name", "last_name"} <= set(by_target):
        first, last = by_target["first_name"], by_target["last_name"]
        suggestions.append(
            Suggestion(
                canonical_object="Employee",
                target="display_name",
                transform="concat",
                args={"paths": [first.source_path, last.source_path]},
                confidence=round(min(first.confidence, last.confidence) * 0.9, 2),
                rationale=f"no single name field; joined from '{first.source_path}' and '{last.source_path}'",
            )
        )
    order = list(_SYNONYMS[canonical_object])
    return sorted(suggestions, key=lambda s: order.index(s.target))


def propose_request_mappings(fields: list[FieldStat], canonical_object: str) -> list[Suggestion]:
    """Write direction: which canonical field fills each field of a request body. The Suggestion's
    source_path is the request field; the manifest's request mapping reads the canonical target."""
    out: list[Suggestion] = []
    for suggestion in propose_mappings(fields, canonical_object):
        if suggestion.source_path is None:
            continue  # a joined name is a read-side construction
        transform: str | None = None
        args: dict[str, Any] = {}
        confidence = suggestion.confidence
        rationale = suggestion.rationale.replace("a name for", "filled from")
        if suggestion.target == "employment_status":
            stat = next(f for f in fields if source_path(f.path) == suggestion.source_path)
            if stat.types == ["boolean"]:
                read_map = suggestion.args.get("map", {})
                truthy = read_map.get("true", "active")
                args = {
                    "map": {status: (status == truthy if truthy == "terminated" else status != "terminated") for status in ("active", "on_leave", "terminated")},
                    "case_insensitive": False,
                }
                transform = "enum_map"
            else:
                inverse: dict[str, str] = {}
                for raw, canon in suggestion.args.get("map", {}).items():
                    inverse.setdefault(canon, raw)
                if not inverse:
                    continue
                transform, args = "enum_map", {"map": inverse}
                confidence = round(confidence * 0.8, 2)
                rationale += "; the reverse of a status mapping is a guess wherever two raw values share a meaning"
        out.append(
            Suggestion(
                canonical_object=canonical_object,
                target=suggestion.target,
                source_path=suggestion.source_path,
                transform=transform,
                args=args,
                confidence=confidence,
                rationale=rationale,
                alternatives=suggestion.alternatives,
            )
        )
    return out
