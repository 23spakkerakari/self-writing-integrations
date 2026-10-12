# ADR 0032: Push sources reach the edge through two nginx routes, off by default

Status: accepted by the founder, 2026-10-11 (two routes, each with its own check) and 2026-10-12
(nginx) (spec 8.1.2, 8.1.6, 14.5, 14.7 "Rate limits"; ADR 0031).

## Context

The gateway already has push routes: `POST /v1/logs` (OTLP) and `POST /webhooks/{source_id}`.
In M1 nothing outside the Compose stack can reach them. Spec 14.5 lets only the reverse proxy
publish a port, and the stack has no proxy. The two kinds of sender also authenticate
differently:

- A customer's OpenTelemetry collector on another host can hold a client certificate from the
  edge-gateway CA, with the `collector` role (ADR 0031).
- A SaaS webhook (Stripe, Shopify) cannot hold a certificate. It signs every request with a
  shared secret, and the gateway already verifies that signature.

## Decision

Two Compose services, each in its own profile, so each route is off until an install turns it
on. Both use the `nginxinc/nginx-unprivileged` image (non-root, BSD licence) with a read-only
root filesystem and a templated configuration in `deploy/compose/proxy/`:

1. **`proxy-otlp` (profile `push-otlp`), TCP passthrough.** It publishes `CARTO_OTLP_PORT`
   (default 4318) and forwards the TLS connection to `edge-gateway:8443` unopened (nginx
   `stream`). The gateway terminates mutual TLS itself, so it sees the collector's own
   certificate: the edge-gateway CA must have signed it, and its `collector` role allows
   `/v1/logs` only. The proxy holds no key for this route.
2. **`proxy-webhooks` (profile `push-webhooks`), HTTPS termination.**
   - It publishes `CARTO_WEBHOOK_PORT` (default 8443) with a server certificate the customer
     provides in `deploy/compose/proxy-tls/`. ACME is not used: the stack has no outbound
     internet by default.
   - It accepts only `POST /webhooks/<source id>`; everything else is 404.
   - It limits each client IP to 10 requests per second with a burst of 50 (429 beyond), and
     bodies to 1 MiB (spec 8.1.6).
   - It forwards to the gateway over mutual TLS with its own client certificate, which has the
     `proxy` role. The gateway allows that role on `/webhooks/{source_id}` only, and still
     checks the provider's signature.
   - Its access log carries the method, the path without the query string, the status and the
     duration, never a header or a body.

`carto-ctl pki init` issues the proxy's certificate (`pki-proxy` volume) when `proxy` is in
`--services`, which the Compose job always passes. `carto-ctl pki issue` (new) signs a
certificate for a collector on another host from a listener's CA. It refuses roles that
listener does not accept. It writes `to-<listener>.crt`, `.key` and `-ca.crt` for that host.
The collector template (`otel/collector.yaml`) points the `otlphttp` exporter at the proxy, with
`tls.server_name_override: edge-gateway` so the gateway's certificate is still checked by name.

## Alternatives

- One port with SNI routing: needs two DNS names and puts the webhook and OTLP exposure
  decisions on one listener.
- Terminating OTLP at the proxy: the gateway would see the proxy's role instead of the
  collector's and could not tell collectors apart.
- Traefik or Caddy (founder chose nginx): Caddy needs a third-party plugin for TCP passthrough;
  Traefik's dynamic configuration is more to review.

## Consequences

- Published ports exist only when a profile is enabled. The proxies sit on a non-internal
  `ingress` network as well as `edge`, and the host firewall decides who reaches them.
- A collector's certificate lives on a customer host. It can only open connections to the
  gateway, and there only `/v1/logs` and health (ADR 0031).
- Webhook replay protection still depends on the sender's timestamp header (threat model).
- CI brings up both profiles. It sends a signed webhook and an OTLP request with a `pki
  issue` certificate through the proxies, checks that both arrive in ClickHouse, and checks
  that unsigned, wrong-path and wrong-certificate requests are refused.
