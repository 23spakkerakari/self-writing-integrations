"""carto_edge.reveal: the internal tokenize and reveal services behind ``/internal/tokenize``
and ``/internal/reveal`` (spec 8.4 "Reveal vault", 12; plan M1 decision 13)."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from carto_common.crypto import (
    InternalAssertion,
    Keyring,
    SigningKey,
    TokenKey,
    generate_key,
    sign_assertion,
    token,
)
from carto_edge.audit import EdgeAudit
from carto_edge.config import RevealSettings
from carto_edge.pipeline.model import FieldClass
from carto_edge.pipeline.tokenize import Tokenizer
from carto_edge.reveal import (
    REVEAL_AUDIENCE,
    REVEAL_PERMISSION,
    SEARCH_PERMISSION,
    TOKENIZE_AUDIENCE,
    AssertionRejected,
    RateLimited,
    RequestTooLarge,
    RevealDisabled,
    RevealError,
    RevealRequest,
    RevealResponse,
    RevealService,
    TokenizeRequest,
    TokenizeResponse,
)
from carto_edge.vault import RevealVault

KEY_V1 = generate_key()
KEY_V2 = generate_key()
KEYRING = Keyring(active=TokenKey(2, KEY_V2), previous=(TokenKey(1, KEY_V1),))
SIGNING = SigningKey.generate()
OTHER_SIGNING = SigningKey.generate()
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
VALUES = ("SO-0004471", "PO-77001", "INV-2026-000123")


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Harness:
    service: RevealService
    vault: RevealVault
    tokenizer: Tokenizer
    audit_path: Path
    clock: FakeClock
    settings: RevealSettings


def raw_token(value: str, version: int = 2) -> str:
    key = KEY_V2 if version == 2 else KEY_V1
    return token(key, version, "id", value)


def make_assertion(
    *,
    subject: str = "user:42",
    permission: str = REVEAL_PERMISSION,
    audience: str = REVEAL_AUDIENCE,
    purpose: str = "ticket INC-1001 for jane@example.com",
    nonce: str | None = None,
    issued_at: datetime = NOW,
    ttl: timedelta = timedelta(seconds=60),
    key: SigningKey = SIGNING,
    request_id: str = "req-1",
) -> str:
    assertion = InternalAssertion(
        subject=subject,
        permission=permission,
        purpose=purpose,
        audience=audience,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
        nonce=nonce or secrets.token_urlsafe(16),
        request_id=request_id,
    )
    return sign_assertion(key, assertion)


def audit_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    clock = FakeClock()
    tokenizer = Tokenizer(KEYRING, "acme")
    vault = RevealVault(tmp_path / "vault.sqlite", generate_key(), "acme")
    result = tokenizer.build_event_identifiers(
        [
            (f"field_{n}", FieldClass.IDENTIFIER, ("raw", "norm", "alnum", "digits"), value)
            for n, value in enumerate(VALUES)
        ],
        expires_at=NOW + timedelta(days=30),
    )
    vault.put_many(result.vault_entries)
    audit = EdgeAudit(tmp_path / "audit.ndjson")
    settings = RevealSettings(values_per_user_per_hour=5, max_tokens_per_request=4)
    service = RevealService(vault, tokenizer, SIGNING.verify_key, settings, audit, clock=clock)
    yield Harness(service, vault, tokenizer, tmp_path / "audit.ndjson", clock, settings)
    audit.close()
    vault.close()


# ---------------------------------------------------------------------------------------------
# reveal
# ---------------------------------------------------------------------------------------------


def test_valid_assertion_reveals_values_and_lists_misses(harness: Harness) -> None:
    unknown = token(KEY_V2, 2, "id", "so0004471")  # alnum form: never in the vault
    request = RevealRequest(
        assertion=make_assertion(),
        tokens=[
            raw_token("SO-0004471"),
            raw_token("SO-0004471", 1),
            raw_token("PO-77001"),
            unknown,
        ],
    )
    response = harness.service.reveal(request)
    assert isinstance(response, RevealResponse)
    assert response.values == {
        raw_token("SO-0004471"): "SO-0004471",
        raw_token("SO-0004471", 1): "SO-0004471",
        raw_token("PO-77001"): "PO-77001",
    }
    assert response.missing == [unknown]
    assert response.remaining_quota == 2


def test_duplicate_tokens_in_one_request_count_once(harness: Harness) -> None:
    request = RevealRequest(
        assertion=make_assertion(), tokens=[raw_token("PO-77001"), raw_token("PO-77001")]
    )
    response = harness.service.reveal(request)
    assert response.values == {raw_token("PO-77001"): "PO-77001"}
    assert response.remaining_quota == 4


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"audience": TOKENIZE_AUDIENCE}, "audience"),
        ({"audience": "something-else"}, "audience"),
        ({"permission": SEARCH_PERMISSION}, "permission"),
        ({"issued_at": NOW - timedelta(minutes=10)}, "expired"),
        ({"issued_at": NOW + timedelta(minutes=10)}, "future"),
        ({"ttl": timedelta(minutes=30)}, "lifetime"),
        ({"key": OTHER_SIGNING}, "signature"),
    ],
)
def test_bad_assertions_are_rejected(
    harness: Harness, kwargs: dict[str, object], reason: str
) -> None:
    request = RevealRequest(assertion=make_assertion(**kwargs), tokens=[raw_token("PO-77001")])  # type: ignore[arg-type]
    with pytest.raises(AssertionRejected, match=reason):
        harness.service.reveal(request)
    assert isinstance(AssertionRejected("x"), RevealError)


def test_malformed_assertion_is_rejected(harness: Harness) -> None:
    with pytest.raises(AssertionRejected):
        harness.service.reveal(RevealRequest(assertion="garbage", tokens=[raw_token("PO-77001")]))


def test_replayed_nonce_is_rejected(harness: Harness) -> None:
    nonce = secrets.token_urlsafe(16)
    first = RevealRequest(assertion=make_assertion(nonce=nonce), tokens=[raw_token("PO-77001")])
    harness.service.reveal(first)
    with pytest.raises(AssertionRejected, match="replay"):
        harness.service.reveal(first)
    again = RevealRequest(assertion=make_assertion(nonce=nonce), tokens=[raw_token("PO-77001")])
    with pytest.raises(AssertionRejected, match="replay"):
        harness.service.reveal(again)
    fresh = RevealRequest(assertion=make_assertion(), tokens=[raw_token("PO-77001")])
    assert harness.service.reveal(fresh).values


def test_seen_nonces_are_forgotten_after_they_expire(harness: Harness) -> None:
    for _ in range(3):
        harness.service.reveal(RevealRequest(assertion=make_assertion(), tokens=[raw_token("x")]))
    assert harness.service.nonce_count == 3
    harness.clock.now += timedelta(minutes=2)
    harness.service.reveal(
        RevealRequest(
            assertion=make_assertion(issued_at=harness.clock.now), tokens=[raw_token("x")]
        )
    )
    assert harness.service.nonce_count == 1


def test_quota_counts_values_returned_per_subject(harness: Harness) -> None:
    def reveal(*values: str, subject: str = "user:42") -> RevealResponse:
        return harness.service.reveal(
            RevealRequest(
                assertion=make_assertion(subject=subject, issued_at=harness.clock.now),
                tokens=[raw_token(value) for value in values],
            )
        )

    assert reveal(*VALUES).remaining_quota == 2
    with pytest.raises(RateLimited) as excinfo:
        reveal(*VALUES)
    assert excinfo.value.retry_after_seconds == 3600
    # the refused call consumed nothing
    assert reveal("SO-0004471", "PO-77001").remaining_quota == 0
    with pytest.raises(RateLimited):
        reveal("SO-0004471")
    # misses are free, even at zero quota
    assert reveal("not-stored").remaining_quota == 0
    # another subject has its own quota
    assert reveal(*VALUES, subject="user:7").remaining_quota == 2
    # the window slides
    harness.clock.now += timedelta(hours=1, seconds=1)
    assert reveal(*VALUES).remaining_quota == 2


def test_rate_limit_is_raised_before_any_value_leaves(harness: Harness) -> None:
    settings = RevealSettings(values_per_user_per_hour=2, max_tokens_per_request=4)
    with EdgeAudit(harness.audit_path.with_name("audit2.ndjson")) as audit:
        service = RevealService(
            harness.vault,
            harness.tokenizer,
            SIGNING.verify_key,
            settings,
            audit,
            clock=harness.clock,
        )
        with pytest.raises(RateLimited) as excinfo:
            service.reveal(
                RevealRequest(assertion=make_assertion(), tokens=[raw_token(v) for v in VALUES])
            )
        assert excinfo.value.retry_after_seconds == 0
        response = service.reveal(
            RevealRequest(
                assertion=make_assertion(), tokens=[raw_token(VALUES[0]), raw_token(VALUES[1])]
            )
        )
        assert len(response.values) == 2 and response.remaining_quota == 0


def test_too_many_tokens_per_request(harness: Harness) -> None:
    tokens = [raw_token(f"V-{n}") for n in range(5)]
    with pytest.raises(RequestTooLarge, match="4"):
        harness.service.reveal(RevealRequest(assertion=make_assertion(), tokens=tokens))


def test_reveal_is_disabled_without_a_verify_key(harness: Harness) -> None:
    with EdgeAudit(harness.audit_path.with_name("audit3.ndjson")) as audit:
        service = RevealService(
            harness.vault, harness.tokenizer, None, harness.settings, audit, clock=harness.clock
        )
        with pytest.raises(RevealDisabled):
            service.reveal(
                RevealRequest(assertion=make_assertion(), tokens=[raw_token("PO-77001")])
            )
        with pytest.raises(RevealDisabled):
            service.tokenize(TokenizeRequest(assertion=make_assertion(), query="SO-0004471"))


def test_every_call_is_audited_without_values(harness: Harness) -> None:
    harness.service.reveal(
        RevealRequest(assertion=make_assertion(), tokens=[raw_token("SO-0004471"), raw_token("zz")])
    )
    with pytest.raises(AssertionRejected):
        harness.service.reveal(
            RevealRequest(assertion=make_assertion(key=OTHER_SIGNING), tokens=[raw_token("x")])
        )
    harness.service.tokenize(
        TokenizeRequest(
            assertion=make_assertion(audience=TOKENIZE_AUDIENCE, permission="search"),
            query="SO-0004471",
        )
    )
    text = harness.audit_path.read_text(encoding="utf-8")
    for value in VALUES:
        assert value not in text
    assert raw_token("SO-0004471") not in text
    assert "jane@example.com" not in text

    rows = audit_rows(harness.audit_path)
    assert [row["action"] for row in rows] == ["reveal", "reveal.rejected", "tokenize"]
    revealed = rows[0]
    assert revealed["actor"] == "user:42"
    assert revealed["request_id"] == "req-1"
    details = revealed["details"]
    assert isinstance(details, dict)
    assert details["permission"] == "reveal"
    assert details["tokens"] == 2
    assert details["revealed"] == 1
    assert details["remaining_quota"] == 4
    assert "INC-1001" in str(details["purpose"]) or "[EMAIL]" in str(details["purpose"])
    rejected = rows[1]["details"]
    assert isinstance(rejected, dict) and "signature" in str(rejected["reason"])
    tokenized = rows[2]["details"]
    assert isinstance(tokenized, dict)
    assert tokenized["tokens"] == 8
    assert "query" not in tokenized
    assert tokenized["query_len"] == 10


# ---------------------------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------------------------


def test_tokenize_returns_tokens_for_all_forms_and_versions(harness: Harness) -> None:
    request = TokenizeRequest(
        assertion=make_assertion(audience=TOKENIZE_AUDIENCE, permission=SEARCH_PERMISSION),
        query="SO-0004471",
    )
    response = harness.service.tokenize(request)
    assert isinstance(response, TokenizeResponse)
    assert response.tokens == harness.tokenizer.tokenize_query("SO-0004471")
    assert len(response.tokens) == 8
    assert response.key_versions == [2, 1]


def test_tokenize_honours_requested_forms(harness: Harness) -> None:
    request = TokenizeRequest(
        assertion=make_assertion(audience=TOKENIZE_AUDIENCE, permission=SEARCH_PERMISSION),
        query="SO-0004471",
        forms=["raw", "digits.0"],
    )
    response = harness.service.tokenize(request)
    assert response.tokens == [
        raw_token("SO-0004471"),
        token(KEY_V2, 2, "id", "4471"),
        raw_token("SO-0004471", 1),
        token(KEY_V1, 1, "id", "4471"),
    ]


def test_tokenize_rejects_reveal_assertions(harness: Harness) -> None:
    with pytest.raises(AssertionRejected, match="audience"):
        harness.service.tokenize(TokenizeRequest(assertion=make_assertion(), query="SO-0004471"))
    with pytest.raises(AssertionRejected, match="permission"):
        harness.service.tokenize(
            TokenizeRequest(
                assertion=make_assertion(audience=TOKENIZE_AUDIENCE), query="SO-0004471"
            )
        )


def test_tokenize_is_rate_limited_per_subject(harness: Harness) -> None:
    def tokenize(subject: str = "user:42") -> TokenizeResponse:
        return harness.service.tokenize(
            TokenizeRequest(
                assertion=make_assertion(
                    subject=subject,
                    audience=TOKENIZE_AUDIENCE,
                    permission=SEARCH_PERMISSION,
                    issued_at=harness.clock.now,
                ),
                query="SO-0004471",
            )
        )

    for _ in range(5):
        tokenize()
    with pytest.raises(RateLimited):
        tokenize()
    tokenize(subject="user:7")
    harness.clock.now += timedelta(hours=1, seconds=1)
    tokenize()


# ---------------------------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------------------------


def test_reveal_request_validates_tokens() -> None:
    with pytest.raises(ValidationError):
        RevealRequest(assertion="a.b", tokens=[])
    with pytest.raises(ValidationError):
        RevealRequest(assertion="a.b", tokens=["SO-0004471"])
    with pytest.raises(ValidationError):
        RevealRequest(assertion="a.b", tokens=[raw_token(str(n)) for n in range(101)])
    with pytest.raises(ValidationError):
        RevealRequest.model_validate({"assertion": "a.b", "tokens": [raw_token("x")], "extra": 1})


def test_tokenize_request_validates_query_and_forms() -> None:
    with pytest.raises(ValidationError):
        TokenizeRequest(assertion="a.b", query="")
    with pytest.raises(ValidationError):
        TokenizeRequest(assertion="a.b", query="x" * 2000)
    with pytest.raises(ValidationError):
        TokenizeRequest(assertion="a.b", query="abc", forms=["soundex"])
    ok = TokenizeRequest(assertion="a.b", query="abc", forms=["digits", "phonetic.1", "raw"])
    assert ok.forms == ["digits", "phonetic.1", "raw"]


def test_response_models_round_trip_as_json() -> None:
    response = RevealResponse(
        values={raw_token("a"): "A-1"}, missing=[raw_token("b")], remaining_quota=3
    )
    assert RevealResponse.model_validate_json(response.model_dump_json()) == response
    with pytest.raises(ValidationError):
        RevealResponse(values={"bad": "A-1"}, missing=[], remaining_quota=0)


def test_rejection_rows_are_capped_and_the_rest_counted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Junk assertions cannot fill the state volume through the audit file (review)."""
    monkeypatch.setattr("carto_edge.reveal.REJECTION_ROWS_PER_HOUR", 3)
    before = len(audit_rows(harness.audit_path)) if harness.audit_path.exists() else 0
    for _ in range(5):
        with pytest.raises(AssertionRejected):
            harness.service.reveal(
                RevealRequest(assertion="garbage", tokens=[raw_token("PO-77001")])
            )
    rows = audit_rows(harness.audit_path)
    assert len(rows) - before == 3
    harness.clock.now = NOW + timedelta(hours=2)
    with pytest.raises(AssertionRejected):
        harness.service.reveal(RevealRequest(assertion="garbage", tokens=[raw_token("PO-77001")]))
    last = audit_rows(harness.audit_path)[-1]
    details = last["details"]
    assert isinstance(details, dict)
    assert details["suppressed_since_last_row"] == 2
