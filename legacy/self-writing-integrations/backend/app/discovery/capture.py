"""Capture: turn observed exchanges into stored samples, with secrets removed first.

Three inputs produce the same thing: a HAR export from a browser or proxy, the RecordingTransport
wrapped around any httpx transport, and the prober. Redaction happens before anything is written:
credential headers keep only their scheme, credential query parameters and credential-named JSON
fields are replaced, and what is stored can be shown to a person or sent to a model.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.db import Database, as_utc, utcnow
from app.discovery.models import SampleSource, SampleSummary, TrafficSample, TrafficSampleRow

REDACTED = "[redacted]"
MAX_BODY_CHARS = 512_000

_SECRET_HEADER = re.compile(r"(authorization|cookie|token|secret|api[-_]?key|password|session|credential|signature)", re.I)
_SECRET_PARAM = re.compile(
    r"^(api[-_]?key|key|token|access[-_]?token|refresh[-_]?token|id[-_]?token|secret|client[-_]?secret|password|passwd|pwd|sig|signature)$",
    re.I,
)
_SECRET_FIELD = re.compile(
    r"(password|passwd|secret|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|private[-_]?key|^token$|^authorization$)",
    re.I,
)
# A basic-auth password this short is a placeholder, not a credential; recording which one tells
# the manifest draft that the API key travels as the username.
_THROWAWAY_PASSWORDS = {"", "x", "X"}
# Hop-by-hop and browser noise that says nothing about the API.
_DROP_HEADERS = {
    "host", "connection", "content-length", "accept-encoding", "user-agent", "referer", "origin", "pragma",
    "cache-control", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-ch-ua", "sec-ch-ua-mobile",
    "sec-ch-ua-platform", "accept-language", "dnt", "upgrade-insecure-requests", "priority", "te",
}


class CapturedExchange(BaseModel):
    """One request/response pair as it was observed, before redaction."""

    method: str
    url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str | None = None
    status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: str | None = None
    captured_at: datetime | None = None


class ImportResult(BaseModel):
    integration: str
    stored: int
    skipped: int
    sample_ids: list[int] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --- redaction ------------------------------------------------------------------------------


def redact_headers(headers: dict[str, str]) -> tuple[dict[str, str], str]:
    """Return headers safe to store and a hint describing how the request authenticated."""
    out: dict[str, str] = {}
    hint = ""
    for name, value in headers.items():
        lower = name.lower()
        if lower in _DROP_HEADERS:
            continue
        if lower == "authorization":
            scheme, _, credential = value.partition(" ")
            scheme = scheme if credential else ""
            out[name] = f"{scheme} {REDACTED}".strip()
            if scheme.lower() == "basic":
                hint = "basic"
                try:
                    _, _, password = base64.b64decode(credential.strip()).decode("utf-8").partition(":")
                    if password in _THROWAWAY_PASSWORDS:
                        hint = f"basic:password={password}"
                except (ValueError, UnicodeDecodeError):
                    pass
            elif scheme.lower() == "bearer":
                hint = "bearer"
            else:
                hint = hint or "header:Authorization"
        elif _SECRET_HEADER.search(lower):
            out[name] = REDACTED
            if "cookie" not in lower and not hint:
                hint = f"header:{name}"
        else:
            out[name] = value
    return out, hint


def redact_url(url: str) -> tuple[str, dict[str, str], str]:
    """Return the URL with credential parameters replaced, its query as a dict, and an auth hint."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    hint = ""
    clean: list[tuple[str, str]] = []
    for name, value in pairs:
        if _SECRET_PARAM.match(name):
            clean.append((name, REDACTED))
            hint = hint or f"query:{name}"
        else:
            clean.append((name, value))
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    safe = urlunsplit((parts.scheme, netloc, parts.path, urlencode(clean), ""))
    return safe, dict(clean), hint


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if _SECRET_FIELD.search(k) and v is not None else redact_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    return value


def redact_body(text: str | None) -> str | None:
    """JSON bodies are redacted field by field. Anything else is kept only if it is short and not a
    form post, because a form body is where credentials travel."""
    if text is None or text == "":
        return None
    if len(text) > MAX_BODY_CHARS:
        return None
    try:
        return json.dumps(redact_json(json.loads(text)))
    except ValueError:
        if "=" in text and "&" in text or _SECRET_FIELD.search(text):
            return None
        return text[:2000]


# --- inputs ---------------------------------------------------------------------------------


def parse_har(har: dict[str, Any], host: str | None = None) -> tuple[list[CapturedExchange], list[str]]:
    """Read the API calls out of a HAR export. Page assets and non-JSON successes are skipped."""
    entries = (har.get("log") or {}).get("entries")
    if not isinstance(entries, list):
        raise ValueError("not a HAR file: log.entries is missing")
    exchanges: list[CapturedExchange] = []
    skipped: dict[str, int] = {}
    for entry in entries:
        request, response = entry.get("request") or {}, entry.get("response") or {}
        url = request.get("url") or ""
        status = int(response.get("status") or 0)
        if not url or not status:
            skipped["incomplete"] = skipped.get("incomplete", 0) + 1
            continue
        if host and (urlsplit(url).hostname or "") != host:
            skipped["other host"] = skipped.get("other host", 0) + 1
            continue
        content = response.get("content") or {}
        body = content.get("text")
        if body is not None and content.get("encoding") == "base64":
            try:
                body = base64.b64decode(body).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                body = None
        mime = (content.get("mimeType") or "").lower()
        if 200 <= status < 300 and "json" not in mime and not _looks_like_json(body):
            skipped["not JSON"] = skipped.get("not JSON", 0) + 1
            continue
        captured_at = None
        if entry.get("startedDateTime"):
            try:
                captured_at = datetime.fromisoformat(str(entry["startedDateTime"]).replace("Z", "+00:00"))
            except ValueError:
                captured_at = None
        exchanges.append(
            CapturedExchange(
                method=str(request.get("method") or "GET").upper(),
                url=url,
                request_headers=_har_headers(request.get("headers")),
                request_body=(request.get("postData") or {}).get("text"),
                status=status,
                response_headers=_har_headers(response.get("headers")),
                response_body=body,
                captured_at=captured_at,
            )
        )
    notes = [f"skipped {count} entr{'y' if count == 1 else 'ies'}: {reason}" for reason, count in skipped.items()]
    return exchanges, notes


def _har_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, list):
        return {}
    return {str(h.get("name")): str(h.get("value", "")) for h in headers if isinstance(h, dict) and h.get("name")}


def _looks_like_json(text: str | None) -> bool:
    return bool(text) and text.lstrip()[:1] in ("{", "[")


class RecordingTransport(httpx.BaseTransport):
    """Wraps any transport and stores every exchange that passes through it. Route a gateway, a
    script or an SDK's HTTP client through it to capture traffic without a separate proxy."""

    def __init__(self, inner: httpx.BaseTransport, store: "TrafficStore", integration: str, source: SampleSource = "proxy") -> None:
        self.inner = inner
        self.store = store
        self.integration = integration
        self.source = source
        self.sample_ids: list[int] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        response = self.inner.handle_request(request)
        response.read()
        exchange = CapturedExchange(
            method=request.method,
            url=str(request.url),
            request_headers=dict(request.headers),
            request_body=body.decode("utf-8", errors="replace") if body else None,
            status=response.status_code,
            response_headers=dict(response.headers),
            response_body=response.text or None,
        )
        self.sample_ids.extend(self.store.add(self.integration, [exchange], self.source).sample_ids)
        return response

    def close(self) -> None:
        self.inner.close()


# --- storage --------------------------------------------------------------------------------


class TrafficStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, integration: str, exchanges: list[CapturedExchange], source: SampleSource = "manual") -> ImportResult:
        result = ImportResult(integration=integration, stored=0, skipped=0)
        with self.db.session() as s:
            rows: list[TrafficSampleRow] = []
            for ex in exchanges:
                parts = urlsplit(ex.url)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    result.skipped += 1
                    continue
                url, query, query_hint = redact_url(ex.url)
                request_headers, header_hint = redact_headers(ex.request_headers)
                response_headers, _ = redact_headers(ex.response_headers)
                row = TrafficSampleRow(
                    integration=integration,
                    method=ex.method.upper(),
                    url=url,
                    host=parts.hostname,
                    path=parts.path or "/",
                    query_json=json.dumps(query),
                    request_headers_json=json.dumps(request_headers),
                    request_body=redact_body(ex.request_body),
                    status=ex.status,
                    response_headers_json=json.dumps(response_headers),
                    response_body=redact_body(ex.response_body),
                    auth_hint=header_hint or query_hint,
                    source=source,
                    captured_at=ex.captured_at or datetime.now(timezone.utc),
                )
                s.add(row)
                rows.append(row)
            s.commit()
            result.stored = len(rows)
            result.sample_ids = [row.id for row in rows]
        return result

    def samples(self, integration: str) -> list[TrafficSample]:
        with self.db.session() as s:
            rows = s.scalars(select(TrafficSampleRow).where(TrafficSampleRow.integration == integration).order_by(TrafficSampleRow.id))
            return [TrafficSample.from_row(r) for r in rows]

    def summaries(self, integration: str) -> list[SampleSummary]:
        return [
            SampleSummary(
                id=x.id,
                method=x.method,
                url=x.url,
                status=x.status,
                source=x.source,
                captured_at=x.captured_at,
                response_bytes=len(x.response_body or ""),
            )
            for x in self.samples(integration)
        ]

    def integrations(self) -> list[tuple[str, int, datetime | None, datetime | None]]:
        with self.db.session() as s:
            rows = s.execute(
                select(
                    TrafficSampleRow.integration,
                    func.count(TrafficSampleRow.id),
                    func.min(TrafficSampleRow.captured_at),
                    func.max(TrafficSampleRow.captured_at),
                )
                .group_by(TrafficSampleRow.integration)
                .order_by(TrafficSampleRow.integration)
            ).all()
            return [(name, count, _aware(first), _aware(last)) for name, count, first, last in rows]

    def clear(self, integration: str) -> int:
        with self.db.session() as s:
            removed = s.execute(delete(TrafficSampleRow).where(TrafficSampleRow.integration == integration)).rowcount
            s.commit()
            return int(removed or 0)


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return as_utc(value) or utcnow()
