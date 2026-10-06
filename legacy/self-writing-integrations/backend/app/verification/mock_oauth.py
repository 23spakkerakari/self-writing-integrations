"""A mock OAuth 2.0 authorization server for offline testing of the broker.

It plays the provider: the simulated user "approves" at the authorization URL (issuing a code
bound to the PKCE challenge), the token endpoint exchanges codes and rotates refresh tokens,
and the API side asks it whether a bearer token is currently valid. Revocation at the
provider is simulated with revoke_all(), which makes the next refresh fail with invalid_grant.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class MockAuthorizationServer:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        access_ttl: int = 3600,
        clock: Callable[[], float] = time.time,
        require_pkce: bool = True,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_ttl = access_ttl
        self.clock = clock
        self.require_pkce = require_pkce
        self._codes: dict[str, dict[str, Any]] = {}
        self._access: dict[str, dict[str, Any]] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, str]] = []
        self.refresh_count = 0
        self.revoked = False

    # --- the user's browser side -------------------------------------------------------

    def authorize(self, authorize_url: str) -> str:
        """Simulate the user approving the consent screen. Returns the redirect URL with the code."""
        parsed = urlparse(authorize_url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if q.get("client_id") != self.client_id:
            raise ValueError("unknown client_id")
        if q.get("response_type") != "code":
            raise ValueError("response_type must be code")
        if self.require_pkce and q.get("code_challenge_method") != "S256":
            raise ValueError("PKCE S256 required")
        code = secrets.token_urlsafe(24)
        self._codes[code] = {
            "scope": q.get("scope", ""),
            "challenge": q.get("code_challenge"),
            "redirect_uri": q["redirect_uri"],
            "expires": self.clock() + 600,
        }
        return q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", "")})

    # --- the token endpoint ------------------------------------------------------------------

    def matches(self, request: httpx.Request) -> bool:
        return str(request.url).split("?")[0] == self.token_url

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if not self._client_ok(request, form):
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self._exchange_code(form)
        if grant == "refresh_token":
            return self._refresh_grant(form)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _client_ok(self, request: httpx.Request, form: dict[str, str]) -> bool:
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            decoded = base64.b64decode(header[6:]).decode()
            cid, _, secret = decoded.partition(":")
            return cid == self.client_id and secret == self.client_secret
        return form.get("client_id") == self.client_id and form.get("client_secret") == self.client_secret

    def _exchange_code(self, form: dict[str, str]) -> httpx.Response:
        code = self._codes.pop(form.get("code", ""), None)
        if code is None or code["expires"] < self.clock():
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "unknown or expired code"})
        if code["redirect_uri"] != form.get("redirect_uri"):
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "redirect_uri mismatch"})
        if self.require_pkce and _s256(form.get("code_verifier", "")) != code["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE verification failed"})
        # A fresh authorization code means the user approved the app again; the revocation is over.
        self.revoked = False
        return self._issue(code["scope"])

    def _refresh_grant(self, form: dict[str, str]) -> httpx.Response:
        token = self._refresh.pop(form.get("refresh_token", ""), None)
        if token is None or self.revoked:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh token revoked or unknown"})
        self.refresh_count += 1
        return self._issue(token["scope"])

    def _issue(self, scope: str) -> httpx.Response:
        access = "at-" + secrets.token_urlsafe(16)
        refresh = "rt-" + secrets.token_urlsafe(16)
        self._access[access] = {"expires": self.clock() + self.access_ttl, "scope": scope}
        self._refresh[refresh] = {"scope": scope}
        body = {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": self.access_ttl, "scope": scope}
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    # --- the API side ----------------------------------------------------------------------

    def is_valid_access_token(self, token: str) -> bool:
        entry = self._access.get(token)
        return entry is not None and entry["expires"] > self.clock() and not self.revoked

    # --- simulation controls -----------------------------------------------------------

    def revoke_all(self) -> None:
        """The user revoked the app at the provider: every token dies, refresh fails from now on."""
        self.revoked = True
        self._access.clear()
        self._refresh.clear()

    def expire_access_tokens(self) -> None:
        for entry in self._access.values():
            entry["expires"] = self.clock() - 1


Handler = Callable[[httpx.Request], httpx.Response]


def composite_transport(routes: list[tuple[Callable[[httpx.Request], bool], Handler]]) -> httpx.MockTransport:
    """Dispatch each request to the first matching handler (token endpoint vs. API mock)."""

    def dispatch(request: httpx.Request) -> httpx.Response:
        for predicate, handler in routes:
            if predicate(request):
                return handler(request)
        return httpx.Response(404, json={"error": f"no mock route for {request.url}"})

    return httpx.MockTransport(dispatch)
