"""The reveal and tokenize services behind ``/internal/reveal`` and ``/internal/tokenize``
(spec 8.4 "Reveal vault", 12; plan M1 decision 13).

Core calls both endpoints over mTLS with a signed, short-lived :class:`InternalAssertion`
(subject, permission, purpose, audience, nonce). :class:`RevealService` verifies the signature
with core's public key (:meth:`carto_edge.keys.KeyManager.load_verify_key`), checks the audience
(``edge-reveal`` or ``edge-tokenize``) and the permission (``reveal`` or ``search``), enforces
one use per nonce, applies the per-subject sliding-window limit of
``RevealSettings.values_per_user_per_hour`` (reveal counts values returned; tokenize counts
queries) and writes one audit row per call (:mod:`carto_edge.audit`) that carries the subject,
permission, purpose and counts, never a value or a token.

Errors are typed so the gateway maps them: :class:`AssertionRejected` to 401,
:class:`RateLimited` to 429, :class:`RequestTooLarge` to 413 and :class:`RevealDisabled` (no
public key installed) to 503. No message carries a value.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from carto_common.crypto import (
    InternalAssertion,
    InvalidAssertionError,
    VerifyKey,
    verify_assertion,
)
from carto_edge.audit import EdgeAudit
from carto_edge.config import RevealSettings
from carto_edge.pipeline.forms import parse_form_request
from carto_edge.pipeline.tokenize import Tokenizer
from carto_edge.vault import RevealVault
from carto_schema.forms import Token

__all__ = [
    "MAX_ASSERTION_LEN",
    "MAX_FORMS_PER_QUERY",
    "MAX_QUERY_LEN",
    "MAX_TOKENS_PER_REQUEST",
    "RATE_WINDOW",
    "REVEAL_AUDIENCE",
    "REVEAL_PERMISSION",
    "SEARCH_PERMISSION",
    "TOKENIZE_AUDIENCE",
    "AssertionRejected",
    "RateLimited",
    "RequestTooLarge",
    "RevealDisabled",
    "RevealError",
    "RevealRequest",
    "RevealResponse",
    "RevealService",
    "TokenizeRequest",
    "TokenizeResponse",
]

REVEAL_AUDIENCE: Final = "edge-reveal"
TOKENIZE_AUDIENCE: Final = "edge-tokenize"
REVEAL_PERMISSION: Final = "reveal"
SEARCH_PERMISSION: Final = "search"
RATE_WINDOW: Final = timedelta(hours=1)
REJECTION_ROWS_PER_HOUR: Final = 600
"""Audit rows for rejected calls per hour; the rest are counted in the next row."""
_REJECTIONS: Final = "*rejections*"
NONCE_GRACE: Final = timedelta(seconds=30)
"""Matches the clock skew :func:`verify_assertion` allows: a nonce is remembered until its
assertion could no longer verify."""
MAX_ASSERTION_LEN: Final = 8192
MAX_QUERY_LEN: Final = 1024
MAX_FORMS_PER_QUERY: Final = 16
MAX_TOKENS_PER_REQUEST: Final = 100
"""Upper bound of ``RevealSettings.max_tokens_per_request``; the configured value applies."""


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------


class RevealError(Exception):
    """Base of the typed failures; messages never carry a value or a token."""


class AssertionRejected(RevealError):  # noqa: N818 - names fixed by the gateway contract (plan M1)
    """Signature, audience, permission, validity window or nonce check failed (401)."""


class RateLimited(RevealError):  # noqa: N818 - names fixed by the gateway contract (plan M1)
    """The subject's hourly quota is exhausted (429)."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RequestTooLarge(RevealError):  # noqa: N818 - names fixed by the gateway contract (plan M1)
    """More tokens than ``max_tokens_per_request`` (413)."""


class RevealDisabled(RevealError):  # noqa: N818 - names fixed by the gateway contract (plan M1)
    """No assertion public key is installed, so nothing can be verified (503)."""


# ---------------------------------------------------------------------------------------------
# Request and response models
# ---------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class TokenizeRequest(_Model):
    """``POST /internal/tokenize``: a search value to the tokens of its forms."""

    assertion: str = Field(min_length=1, max_length=MAX_ASSERTION_LEN)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    forms: list[str] | None = Field(default=None, max_length=MAX_FORMS_PER_QUERY)

    @field_validator("forms")
    @classmethod
    def _known_forms(cls, forms: list[str] | None) -> list[str] | None:
        if forms is not None:
            for name in forms:
                parse_form_request(name)
        return forms


class TokenizeResponse(_Model):
    tokens: list[Token]
    key_versions: list[int]


class RevealRequest(_Model):
    """``POST /internal/reveal``: tokens to values."""

    assertion: str = Field(min_length=1, max_length=MAX_ASSERTION_LEN)
    tokens: list[Token] = Field(min_length=1, max_length=MAX_TOKENS_PER_REQUEST)


class RevealResponse(_Model):
    values: dict[Token, str]
    missing: list[Token]
    remaining_quota: int = Field(ge=0)


# ---------------------------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------------------------


class _SlidingWindow:
    """Per-subject counts inside the last :data:`RATE_WINDOW`. Not thread-safe by itself."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, deque[tuple[datetime, int]]] = {}

    def _bucket(self, subject: str, now: datetime) -> deque[tuple[datetime, int]]:
        bucket = self._entries.setdefault(subject, deque())
        cutoff = now - RATE_WINDOW
        while bucket and bucket[0][0] <= cutoff:
            bucket.popleft()
        if not bucket:
            self._entries.pop(subject, None)
        return bucket

    def used(self, subject: str, now: datetime) -> int:
        return sum(count for _moment, count in self._bucket(subject, now))

    def add(self, subject: str, now: datetime, count: int) -> None:
        if count > 0:
            self._entries.setdefault(subject, deque()).append((now, count))

    def retry_after(self, subject: str, now: datetime) -> int:
        bucket = self._bucket(subject, now)
        if not bucket:
            return 0
        return max(0, int((bucket[0][0] + RATE_WINDOW - now).total_seconds()))


# ---------------------------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RevealService:
    """Verify, rate-limit, audit and answer reveal and tokenize calls."""

    __slots__ = (
        "_audit",
        "_clock",
        "_lock",
        "_nonces",
        "_reject_lock",
        "_rejections",
        "_reveal_window",
        "_settings",
        "_suppressed",
        "_tokenize_window",
        "_tokenizer",
        "_vault",
        "_verify_key",
    )

    def __init__(
        self,
        vault: RevealVault,
        tokenizer: Tokenizer,
        verify_key: VerifyKey | None,
        settings: RevealSettings,
        audit: EdgeAudit,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._vault = vault
        self._tokenizer = tokenizer
        self._verify_key = verify_key
        self._settings = settings
        self._audit = audit
        self._clock = clock if clock is not None else _utc_now
        self._lock = threading.Lock()
        self._nonces: dict[str, datetime] = {}
        self._reveal_window = _SlidingWindow()
        self._tokenize_window = _SlidingWindow()
        self._rejections = _SlidingWindow()
        self._suppressed = 0
        self._reject_lock = threading.Lock()

    def __repr__(self) -> str:
        enabled = self._verify_key is not None
        return f"RevealService(enabled={enabled}, tokenizer={self._tokenizer!r})"

    @property
    def enabled(self) -> bool:
        return self._verify_key is not None

    @property
    def nonce_count(self) -> int:
        """Nonces currently remembered (tests and status)."""
        with self._lock:
            return len(self._nonces)

    # -----------------------------------------------------------------------------------------
    # Assertion checks
    # -----------------------------------------------------------------------------------------

    def _reject(self, action: str, reason: str, subject: str = "", request_id: str = "") -> None:
        """Audit a rejection; beyond :data:`REJECTION_ROWS_PER_HOUR` rows the rest are counted
        and reported on the next row written, so junk calls cannot fill the state volume."""
        now = self._clock()
        # Its own lock: _verify calls this while holding self._lock (nonce replay).
        with self._reject_lock:
            if self._rejections.used(_REJECTIONS, now) >= REJECTION_ROWS_PER_HOUR:
                self._suppressed += 1
                return
            self._rejections.add(_REJECTIONS, now, 1)
            suppressed, self._suppressed = self._suppressed, 0
        details: dict[str, object] = {"reason": reason}
        if suppressed:
            details["suppressed_since_last_row"] = suppressed
        self._audit.record(f"{action}.rejected", subject, action, details, request_id)

    def _verify(
        self, text: str, *, action: str, audience: str, permission: str
    ) -> InternalAssertion:
        if self._verify_key is None:
            msg = "no assertion public key is installed; the endpoint is disabled"
            raise RevealDisabled(msg)
        now = self._clock()
        try:
            assertion = verify_assertion(self._verify_key, text, audience=audience, now=now)
        except InvalidAssertionError as exc:
            self._reject(action, str(exc))
            raise AssertionRejected(str(exc)) from exc
        if assertion.permission != permission:
            msg = "assertion permission mismatch"
            self._reject(action, msg, assertion.subject, assertion.request_id)
            raise AssertionRejected(msg)
        with self._lock:
            expired = [nonce for nonce, until in self._nonces.items() if until + NONCE_GRACE < now]
            for nonce in expired:
                del self._nonces[nonce]
            if assertion.nonce in self._nonces:
                msg = "assertion replayed (nonce already used)"
                self._reject(action, msg, assertion.subject, assertion.request_id)
                raise AssertionRejected(msg)
            self._nonces[assertion.nonce] = assertion.expires_at
        return assertion

    # -----------------------------------------------------------------------------------------
    # Reveal
    # -----------------------------------------------------------------------------------------

    def reveal(self, request: RevealRequest) -> RevealResponse:
        """Values of the requested tokens for a verified subject, within quota."""
        assertion = self._verify(
            request.assertion,
            action="reveal",
            audience=REVEAL_AUDIENCE,
            permission=REVEAL_PERMISSION,
        )
        limit = self._settings.values_per_user_per_hour
        tokens = list(dict.fromkeys(request.tokens))
        if len(tokens) > self._settings.max_tokens_per_request:
            msg = f"at most {self._settings.max_tokens_per_request} tokens per request"
            self._reject("reveal", msg, assertion.subject, assertion.request_id)
            raise RequestTooLarge(msg)
        now = self._clock()
        found = self._vault.reveal(tokens, now=now)
        with self._lock:
            remaining = limit - self._reveal_window.used(assertion.subject, now)
            if len(found) > max(remaining, 0):
                retry_after = self._reveal_window.retry_after(assertion.subject, now)
                self._audit.record(
                    "reveal.rate_limited",
                    assertion.subject,
                    "vault",
                    {
                        "permission": assertion.permission,
                        "purpose": assertion.purpose,
                        "tokens": len(tokens),
                        "would_reveal": len(found),
                        "remaining_quota": max(remaining, 0),
                        "retry_after_seconds": retry_after,
                    },
                    assertion.request_id,
                )
                msg = f"reveal quota of {limit} values per hour exhausted for this subject"
                raise RateLimited(msg, retry_after)
            self._reveal_window.add(assertion.subject, now, len(found))
            remaining_after = max(remaining, 0) - len(found)
        missing = [token_text for token_text in tokens if token_text not in found]
        self._audit.record(
            "reveal",
            assertion.subject,
            "vault",
            {
                "permission": assertion.permission,
                "purpose": assertion.purpose,
                "tokens": len(tokens),
                "revealed": len(found),
                "missing": len(missing),
                "remaining_quota": remaining_after,
            },
            assertion.request_id,
        )
        return RevealResponse(values=found, missing=missing, remaining_quota=remaining_after)

    # -----------------------------------------------------------------------------------------
    # Tokenize
    # -----------------------------------------------------------------------------------------

    def tokenize(self, request: TokenizeRequest) -> TokenizeResponse:
        """Tokens of a search value under every form and key version (spec 12)."""
        assertion = self._verify(
            request.assertion,
            action="tokenize",
            audience=TOKENIZE_AUDIENCE,
            permission=SEARCH_PERMISSION,
        )
        limit = self._settings.values_per_user_per_hour
        now = self._clock()
        with self._lock:
            used = self._tokenize_window.used(assertion.subject, now)
            if used >= limit:
                retry_after = self._tokenize_window.retry_after(assertion.subject, now)
                self._audit.record(
                    "tokenize.rate_limited",
                    assertion.subject,
                    "tokenizer",
                    {
                        "permission": assertion.permission,
                        "purpose": assertion.purpose,
                        "query_len": len(request.query),
                        "retry_after_seconds": retry_after,
                    },
                    assertion.request_id,
                )
                msg = f"tokenize quota of {limit} queries per hour exhausted for this subject"
                raise RateLimited(msg, retry_after)
            self._tokenize_window.add(assertion.subject, now, 1)
        tokens = self._tokenizer.tokenize_query(request.query, request.forms)
        self._audit.record(
            "tokenize",
            assertion.subject,
            "tokenizer",
            {
                "permission": assertion.permission,
                "purpose": assertion.purpose,
                "query_len": len(request.query),
                "forms": list(request.forms) if request.forms is not None else None,
                "tokens": len(tokens),
                "key_versions": self._tokenizer.key_versions,
            },
            assertion.request_id,
        )
        return TokenizeResponse(tokens=tokens, key_versions=self._tokenizer.key_versions)
