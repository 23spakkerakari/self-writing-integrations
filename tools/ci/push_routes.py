#!/usr/bin/env python3
"""Exercise the push routes through the Compose proxies (ADR 0031, 0032). Standard library only.

    push_routes.py webhook --port 8443 --ca proxy-tls/tls.crt --secret-file F --source ID
    push_routes.py otlp --port 4318 --pki DIR --source ID [--marker TEXT]

``webhook`` posts a signed webhook through proxy-webhooks and checks that unsigned, wrongly
signed, non-POST and off-route requests are refused there or at the gateway. ``otlp`` sends one
OTLP log record through proxy-otlp with a ``carto-ctl pki issue`` collector certificate (``DIR``
holds ``to-edge-gateway.crt``, ``.key`` and ``-ca.crt``), checks that the gateway accepted it, and
that the same certificate is refused on the internal routes and without a certificate at all.
Exit 0 when every check holds; each check prints one line.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import socket
import ssl
import struct
import sys
import time
from pathlib import Path

HOST = "localhost"
GATEWAY_NAME = "edge-gateway"
"""The name in the gateway's certificate; collectors set it as the TLS server name."""

# ---------------------------------------------------------------------------------------------
# A minimal OTLP/protobuf encoder (opentelemetry.proto.collector.logs.v1.ExportLogsServiceRequest)
# ---------------------------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    """A length-delimited field (wire type 2)."""
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _string_value(text: str) -> bytes:
    return _field(1, text.encode("utf-8"))  # AnyValue.string_value


def _key_value(key: str, value: str) -> bytes:
    return _field(1, key.encode("utf-8")) + _field(2, _string_value(value))


def otlp_request(source_id: str, body: str, time_unix_nano: int) -> bytes:
    """``ExportLogsServiceRequest`` with one record of ``body`` for ``source_id``."""
    resource = _field(1, _key_value("carto.source_id", source_id))  # Resource.attributes
    record = (
        _varint(1 << 3 | 1)
        + struct.pack("<Q", time_unix_nano)  # LogRecord.time_unix_nano
        + _field(5, _string_value(body))  # LogRecord.body
    )
    scope_logs = _field(2, record)  # ScopeLogs.log_records
    resource_logs = _field(1, resource) + _field(2, scope_logs)
    return _field(1, resource_logs)  # ExportLogsServiceRequest.resource_logs


# ---------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------


class CheckFailedError(Exception):
    pass


def _check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        raise CheckFailedError(what)


def _request(
    context: ssl.SSLContext,
    port: int,
    method: str,
    path: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    server_name: str = HOST,
) -> tuple[int, bytes]:
    raw = socket.create_connection((HOST, port), timeout=15)
    tls = context.wrap_socket(raw, server_hostname=server_name)
    connection = http.client.HTTPSConnection(HOST, port, timeout=15, context=context)
    connection.sock = tls
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def check_webhook(port: int, ca: Path, secret: str, source_id: str) -> None:
    context = ssl.create_default_context(cafile=str(ca))
    path = f"/webhooks/{source_id}"
    payload = {
        "type": "payment.succeeded",
        "id": "evt_ci_push_route",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    body = json.dumps(payload).encode("utf-8")
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), stamp.encode() + b"." + body, hashlib.sha256)
    headers = {
        "Content-Type": "application/json",
        "X-Carto-Timestamp": stamp,
        "X-Carto-Signature": "sha256=" + signature.hexdigest(),
    }
    status, answer = _request(context, port, "POST", path, body, headers)
    _check(status == 202, f"signed webhook accepted through the proxy (got {status})")
    _check(json.loads(answer).get("accepted") == 1, "the gateway reports one record")
    unsigned = {"Content-Type": "application/json"}
    status, _ = _request(context, port, "POST", path, body, unsigned)
    _check(status == 401, f"unsigned webhook refused by the gateway (got {status})")
    wrong = dict(headers, **{"X-Carto-Signature": "sha256=" + "0" * 64})
    status, _ = _request(context, port, "POST", path, body, wrong)
    _check(status == 401, f"wrongly signed webhook refused (got {status})")
    status, _ = _request(context, port, "GET", path)
    _check(status == 403, f"GET refused by the proxy (got {status})")
    for other in ("/internal/reveal", "/v1/logs", "/webhooks/Upper", "/healthz", "/"):
        status, _ = _request(context, port, "POST", other, b"{}", unsigned)
        _check(status == 404, f"POST {other} is not routed by the proxy (got {status})")


def check_otlp(port: int, pki: Path, source_id: str, marker: str) -> None:
    context = ssl.create_default_context(cafile=str(pki / "to-edge-gateway-ca.crt"))
    context.load_cert_chain(str(pki / "to-edge-gateway.crt"), str(pki / "to-edge-gateway.key"))
    record = json.dumps(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": "info",
            "msg": marker,
        }
    )
    body = otlp_request(source_id, record, time.time_ns())
    headers = {"Content-Type": "application/x-protobuf"}
    status, answer = _request(context, port, "POST", "/v1/logs", body, headers, GATEWAY_NAME)
    _check(status == 200, f"OTLP through the passthrough accepted (got {status})")
    _check(answer == b"", "no partial success: the record was not rejected")
    for path in ("/internal/reveal", "/internal/tokenize", "/webhooks/src_shop_hooks"):
        status, _ = _request(context, port, "POST", path, b"{}", {}, GATEWAY_NAME)
        _check(status == 403, f"the collector role is refused on {path} (got {status})")
    status, _ = _request(context, port, "GET", "/healthz", b"", {}, GATEWAY_NAME)
    _check(status == 200, f"the collector certificate reaches health (got {status})")
    anonymous = ssl.create_default_context(cafile=str(pki / "to-edge-gateway-ca.crt"))
    try:
        _request(anonymous, port, "GET", "/healthz", b"", {}, GATEWAY_NAME)
    except (ssl.SSLError, ConnectionError, http.client.HTTPException, OSError):
        refused = True
    else:
        refused = False
    _check(refused, "without a client certificate the gateway refuses the handshake")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    webhook = commands.add_parser("webhook")
    webhook.add_argument("--port", type=int, default=8443)
    webhook.add_argument("--ca", type=Path, required=True)
    webhook.add_argument("--secret-file", type=Path, required=True)
    webhook.add_argument("--source", required=True)
    otlp = commands.add_parser("otlp")
    otlp.add_argument("--port", type=int, default=4318)
    otlp.add_argument("--pki", type=Path, required=True)
    otlp.add_argument("--source", required=True)
    otlp.add_argument("--marker", default="ci push route check")
    args = parser.parse_args(argv)
    try:
        if args.command == "webhook":
            secret = args.secret_file.read_text(encoding="utf-8").strip()
            check_webhook(args.port, args.ca, secret, args.source)
        else:
            check_otlp(args.port, args.pki, args.source, args.marker)
    except CheckFailedError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
