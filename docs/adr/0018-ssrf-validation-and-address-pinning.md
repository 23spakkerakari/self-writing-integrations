# ADR 0018: SSRF validation and address pinning per connector transport

Status: accepted, 2026-10-08 (spec 8.1 "Hostnames are validated against SSRF rules", 14.7, 15 threat 6)

## Context

Spec 14.7 requires resolving connector and channel host names, rejecting loopback, link-local
(including the cloud metadata address), the cluster's own ranges and any private range not in
the admin's allow-list, and pinning the resolved address to defeat DNS rebinding. Connectors use
four transports (HTTP, PostgreSQL, MySQL, SQL Server, SSH), each with its own idea of how to
connect by name.

## Decision

`carto_edge.net.ssrf.DefaultNetworkPolicy.resolve(host, port)` performs the resolution and the
checks once per connection attempt and returns the pinned address. Pinning per transport:

| Transport | Pinning |
| --- | --- |
| HTTP (Splunk, Vault, webhooks' outbound test) | `PinnedAsyncTransport`: an `httpcore` network backend that opens the TCP connection to the pinned address while TLS verifies the original host name |
| PostgreSQL | `hostaddr=<pinned>` with `host=<name>`, so `sslmode=verify-full` still checks the name |
| SFTP | `asyncssh.connect(<pinned address>)` with the host key verified against the pinned SHA-256 fingerprint; the name is not needed for trust |
| MySQL, SQL Server | validated at every connect; the driver connects by name (no address pinning in PyMySQL or ODBC without losing TLS name checks) |

A name that resolves to any rejected address is refused entirely (no partial trust in a mixed
answer). Literal IP addresses go through the same checks.

## Alternatives

- Validate only at configuration time: defeated by rebinding, which the spec names explicitly.
- A local resolver cache for all transports: MySQL and ODBC drivers resolve internally; the
  cache cannot be injected without patching them.

## Consequences

MySQL and SQL Server sources carry a residual rebinding risk between validation and connect; the
install guide recommends host entries or the allow-list for them. The policy is built from
`network.allowed_source_cidrs` in the sources file (Appendix A).
