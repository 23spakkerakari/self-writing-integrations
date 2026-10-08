# ADR 0019: The SQL Server connector is tested against a fake connection in M1

Status: accepted, 2026-10-08 (spec 8.1.4, 18.2, 21 "PostgreSQL first, then SQL Server and MySQL")

## Context

Spec 18.2 asks for testcontainers-backed integration tests. The SQL Server image is about 1.5 GB
and needs the Microsoft ODBC driver on the test host; GitHub's Ubuntu runners do not ship it,
and installing it adds minutes to every CI run for a connector no design partner uses yet.

## Decision

The SQL Server dialect (`ApplicationIntent=ReadOnly`, `Encrypt=yes`, query timeout,
`HAS_PERMS_BY_NAME` and `IS_SRVROLEMEMBER('sysadmin')` checks) is implemented and unit-tested
against a fake connection that records the connection string and the statements issued.
PostgreSQL and MySQL run the full read-only tests under testcontainers. A SQL Server container
test is scheduled for the first install that needs the connector (the payer partner, Section 21),
with the ODBC driver installed in that CI job only.

## Alternatives

- Run the container test now: slow CI for every PR, ODBC driver provisioning in the workflow.
- Drop SQL Server from M1: the spec lists it, and the dialect code is small once the statement
  check and the grant check exist for the other two engines.

## Consequences

`docs/install` marks SQL Server support as "implemented, verified against a live server at the
first deployment". The read-only grant script for SQL Server ships with the other two.
