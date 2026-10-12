# ADR 0031: Mutual TLS routes accept only the service roles listed for them

Status: accepted by the founder, 2026-10-11, and amended 2026-10-12 with a CA per listener (see
the amendment below) (spec 14.4, 12 "Internal", 15). Built in M1, before the push routes (ADR
0032) issue certificates to customer collectors.

## Context

Every internal listener (edge-gateway, ingest-api) requires a client certificate signed by the
install's private CA, and accepts any such certificate on any route. In M1 that let the
otel-collector's certificate call `/internal/reveal` on the edge, or post batches to ingest-api
as if it were an edge gateway. Two more facts make it worse:

- In Compose every service mounts the whole `pki` volume read-only, so each one can read every
  other service's private key. Checking which certificate a caller presents would not prove
  which service it is.
- The push routes (founder decision of 2026-10-11) will issue install certificates to
  collectors on customer hosts, outside the stack.

## Decision

**A role per certificate.** A certificate names its role in a URI subject alternative name
`urn:carto:role:<role>`. A certificate with no such name, or with more than one, has no role.
The roles are fixed in code: `edge-gateway`, `ingest-api`, `otel-collector` (the collector in
the stack), `collector` (a collector on a customer host, ADR 0032), `proxy` (the reverse proxy,
ADR 0032) and `api` (core's API, M3). `carto-ctl pki init` gives each service certificate the
role named like the service. cert-manager does the same with `uris:` in M6.

**Each route lists its roles, and anything unlisted is refused.** An ASGI middleware, installed
by each app's factory so no deployment can leave it out, reads the role of the connection's
verified certificate and answers 403 (an RFC 7807 problem that names no certificate detail)
when the route does not list it:

| Listener | Route | Roles |
| --- | --- | --- |
| edge-gateway | `POST /v1/logs` | `otel-collector`, `collector` |
| edge-gateway | `POST /webhooks/{source_id}` | `proxy` (the sender is authenticated by its signature) |
| edge-gateway | `POST /internal/tokenize`, `POST /internal/reveal` | `api` |
| ingest-api | `POST /internal/ingest`, `POST /internal/heartbeat` | `edge-gateway` |
| both | `GET /healthz`, `/readyz`, `/metrics` | any certificate from the install CA |

The policy is code, not configuration, so a settings file cannot widen it. A refusal is logged
with the method, the route template and the presented role, and counted in
`carto_edge_mtls_refused_total` (by route) or `carto_ingest_mtls_refused_total`.

**How the role reaches the app.** uvicorn does not pass the client certificate to the ASGI
app. Both servers already use uvicorn's h11 protocol; a small subclass reads the verified peer
certificate when the connection is made and puts its role into the request scope's `state`. A
real-server test on each listener pins this against uvicorn upgrades.

**Each service holds only its own key.** `carto-ctl pki init --layout per-service` writes
`<out>/<service>/tls.crt`, `tls.key` and `ca.crt`. Compose gives each service its own volume
holding only its directory, and the CA key stays on its own volume as before.

## Alternatives

- The common name as identity: it is free text and deprecated for identity checks; a URI SAN
  cannot be mistaken for a host name the certificate serves.
- A CA or a listener per role: more keys and ports for the same check.
- Identity passed by the proxy in a header: the services would have to trust the proxy for
  every caller, and internal calls do not go through it.

## Consequences

- Certificates from an earlier `pki init` have no role and are refused on every data route.
  M1 has no installation yet; the Compose job and CI create fresh certificates.
- `/webhooks` accepts only the proxy, which arrives with ADR 0032. Until then webhook sources
  cannot be reached, which matches M1 (no published port).
- `/internal/tokenize` and `/internal/reveal` keep their own signed assertions (spec 9, the
  reveal service) in addition to the role check.
- In-process tests wrap the app with the role they act as.

## Amendment, 2026-10-12: a CA per listener, as well as roles

The founder chose both: route roles as above, and a separate CA for each listener.

**One CA per listener.** Each listener (`ingest-api`, `edge-gateway`; core's API from M3) has
its own CA. It signs that listener's server certificate and the client certificates of the
services allowed to call it, and the listener trusts only that CA for client certificates. A
caller signed by another listener's CA is refused during the TLS handshake, before any route is
read. Who may call which listener is fixed in `carto-ctl`:

| Listener | Its CA signs client certificates for |
| --- | --- |
| `ingest-api` | `edge-gateway` |
| `edge-gateway` | `otel-collector`, `collector`, `proxy`, `api` |

The role checks stay, because one listener serves several callers (the gateway takes OTLP from
collectors and reveal calls from core's API), and a CA cannot tell its routes apart. Together:
a certificate issued to a collector on a customer host can never open a connection to
`ingest-api`, and on the gateway it can call only `/v1/logs` and health.

**Files.** `carto-ctl pki init` always writes one directory per service (the `--layout`
option is gone):

| File in `<out>/<service>/` | What it is |
| --- | --- |
| `listener.crt`, `listener.key` | the service's server certificate, signed by its own listener CA (also usable as a client certificate to its own health route) |
| `listener-ca.crt` | its listener CA, which it trusts for client certificates |
| `to-<listener>.crt`, `to-<listener>.key` | its client certificate for calling `<listener>`, signed by that listener's CA, with its role |
| `to-<listener>-ca.crt` | that listener's CA, to verify the server |

The CA keys are written as `<listener>-ca.key` (with `<listener>-ca.crt`) to `--ca-key-dir`,
which no running service mounts. Compose keeps one volume per service.

**Consequences.** Two CAs to create and rotate in the Compose stack instead of one (manual until
M6, when cert-manager runs one issuer per listener). Certificates for collectors on customer
hosts (ADR 0032) are signed by the `edge-gateway` CA only.
