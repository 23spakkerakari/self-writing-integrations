"""Spec 18.1 property: the classifier never emits a raw value from a field classified as
identifier, person, amount, secret or quarantined.

Hypothesis drives field names, value populations and admin pins; the assertions are the policy
invariants of spec 2.3 (2, 3) and 8.3, plus "no value ever reaches a reason, a log line or a
summary of a non-kept field".
"""

from __future__ import annotations

import base64
import json
import uuid

import structlog
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from carto_edge.config import ClassifySettings, FieldPolicyPin
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import FieldClass, Policy, field_ref
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.stats import FieldStatsStore

SYS = "sys_prop"
TPL = "tpl_0000000000ff"

NEVER_KEPT = frozenset(
    {
        FieldClass.IDENTIFIER,
        FieldClass.PERSON_NAME,
        FieldClass.CONTACT,
        FieldClass.GOVERNMENT_ID,
        FieldClass.FINANCIAL,
        FieldClass.HEALTH,
        FieldClass.AMOUNT,
        FieldClass.SECRET_LIKE,
        FieldClass.UNKNOWN,
    }
)

FIELD_NAMES = [
    "order_id",
    "po_num",
    "status",
    "customer_name",
    "cardholderName",
    "customer_email",
    "ship_to_address",
    "ssn",
    "total",
    "order_date",
    "ts",
    "password",
    "api_key",
    "msg.param_0",
    "note",
    "warehouse_code",
    "username",
    "latency",
    "payload.auth.token",
    "diagnosis",
]

PIN_CLASSES = [
    "identifier",
    "low_card_attribute",
    "timestamp",
    "amount",
    "date",
    "person_name",
    "contact",
    "government_id",
    "financial",
    "health",
    "free_text",
    "secret_like",
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(seed: int) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"sub": f"s{seed}"}).encode())
    return f"{header}.{claims}.{_b64url(bytes(range(seed % 11, seed % 11 + 32)))}"


def _population(kind: str, n: int, seed: int) -> list[str]:
    firsts = ["Maria", "Alex", "Priya", "Jonas", "Amara", "Luis", "Hannah", "Omar"]
    lasts = ["Lopez", "Chen", "Patel", "Weber", "Okafor", "Garcia", "Schmidt", "Haddad"]
    words = ["the", "quick", "order", "was", "released", "from", "warehouse", "after", "payment"]
    if kind == "int_ids":
        return [str(4471 + seed + i) for i in range(n)]
    if kind == "ref_ids":
        return [f"SO-{seed + i:07d}" for i in range(n)]
    if kind == "uuids":
        return [str(uuid.UUID(int=(seed + i) * 7919)) for i in range(n)]
    if kind == "names":
        return [f"{firsts[(seed + i) % 8]} {lasts[(seed + i * 3) % 8]}" for i in range(n)]
    if kind == "emails":
        return [f"user{seed + i}@example.org" for i in range(n)]
    if kind == "ssns":
        return [f"{100 + (seed + i) % 500:03d}-{10 + i % 80:02d}-{1000 + i:04d}" for i in range(n)]
    if kind == "dates":
        return [f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]
    if kind == "timestamps":
        return [f"2026-10-{(i % 28) + 1:02d}T{i % 24:02d}:{i % 60:02d}:00Z" for i in range(n)]
    if kind == "amounts":
        return [f"{((seed + i) * 37) % 900 + 10}.{(i * 13) % 100:02d}" for i in range(n)]
    if kind == "sentences":
        return [
            " ".join(words[(i + j) % len(words)] for j in range(7)) + f" #{i}" for i in range(n)
        ]
    if kind == "enum":
        return [["CREATED", "RELEASED", "SHIPPED"][(seed + i) % 3] for i in range(n)]
    if kind == "secrets":
        return [_jwt(seed + i) for i in range(n)]
    if kind == "floats":
        return [f"0.{(seed + i) % 1_000_000:06d}" for i in range(n)]
    msg = kind
    raise AssertionError(msg)


KINDS = [
    "int_ids",
    "ref_ids",
    "uuids",
    "names",
    "emails",
    "ssns",
    "dates",
    "timestamps",
    "amounts",
    "sentences",
    "enum",
    "secrets",
    "floats",
]


@st.composite
def pins(draw: st.DrawFn, path: str) -> list[FieldPolicyPin]:
    if not draw(st.booleans()):
        return []
    field_class = draw(st.sampled_from(PIN_CLASSES))
    if field_class == "secret_like":
        policy = "drop"
    elif field_class in {"person_name", "contact", "government_id", "financial", "health"}:
        policy = draw(st.sampled_from(["tokenize", "drop"]))
    else:
        policy = draw(st.sampled_from(["keep", "tokenize", "drop"]))
    forms = draw(
        st.one_of(
            st.none(),
            st.lists(
                st.sampled_from(["raw", "norm", "alnum", "digits", "date", "amount", "phonetic"]),
                min_size=1,
                max_size=3,
                unique=True,
            ),
        )
    )
    target = draw(st.sampled_from([f"{SYS}/*/{path}", field_ref(SYS, TPL, path)]))
    return [FieldPolicyPin(field=target, field_class=field_class, policy=policy, forms=forms)]  # type: ignore[arg-type]


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_policy_invariants(data: st.DataObject) -> None:
    path = data.draw(st.sampled_from(FIELD_NAMES))
    kind = data.draw(st.sampled_from(KINDS))
    n = data.draw(st.integers(min_value=0, max_value=400))
    seed = data.draw(st.integers(min_value=0, max_value=10_000))
    quarantine = data.draw(st.sampled_from([20, 200]))
    field_pins = data.draw(pins(path))
    values = _population(kind, n, seed)
    assume(all(len(value) <= 256 for value in values))

    config = ClassifySettings(sample_values=32, quarantine_samples=quarantine)
    classifier = Classifier(
        config, field_pins, RegexDetector(), FieldStatsStore.from_settings(config)
    )
    ref = field_ref(SYS, TPL, path)
    for value in values:
        classifier.observe(ref, path, value)
    with structlog.testing.capture_logs() as logs:
        decision = classifier.decide(ref, path)
        summary = classifier.field_summary(ref)

    # Spec 18.1: never a raw value from identifier, PII, amount, secret or quarantined fields.
    # An admin pin may keep a low-risk identifier in clear (spec 8.3); the rules never do.
    if decision.field_class in NEVER_KEPT and not decision.pinned:
        assert decision.policy is not Policy.KEEP
    # Spec 8.3 rule 1: secrets are always dropped, whether the rule or a pin said so.
    if decision.field_class is FieldClass.SECRET_LIKE:
        assert decision.policy is Policy.DROP
        assert decision.forms == ()
    if kind == "secrets" and n > 0:
        assert decision.field_class is FieldClass.SECRET_LIKE
    # Spec 2.3 invariant 3: too few samples means tokenize-if-shaped or drop (unless pinned
    # or decided by a name or shape rule that does not need statistics).
    if n < quarantine and not decision.pinned and decision.field_class is FieldClass.UNKNOWN:
        assert decision.reason == "quarantine"
    # Policy and forms agree.
    if decision.policy is Policy.TOKENIZE:
        assert decision.forms
    else:
        assert decision.forms == ()
    # Pins never keep PII.
    if decision.pinned and decision.policy is Policy.KEEP:
        assert decision.field_class in {
            FieldClass.IDENTIFIER,
            FieldClass.LOW_CARD_ATTRIBUTE,
            FieldClass.TIMESTAMP,
            FieldClass.AMOUNT,
            FieldClass.DATE,
            FieldClass.FREE_TEXT,
        }
    # No value in the reason, the summary of a non-kept field, or any log line.
    leaks = [value for value in values if len(value) >= 3]
    assert not any(value in decision.reason for value in leaks)
    assert summary["sample_values"] == [] or decision.policy is Policy.KEEP
    if decision.policy is not Policy.KEEP:
        rendered = json.dumps(summary)
        assert not any(value in rendered for value in leaks)
    rendered_logs = json.dumps(logs, default=str)
    assert not any(value in rendered_logs for value in leaks)
    assert decision.samples_seen == n
