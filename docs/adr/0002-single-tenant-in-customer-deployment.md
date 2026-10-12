# ADR 0002: Single-tenant deployment inside the customer's environment, with an edge/core split

Status: accepted, 2026-10-06 (spec Section 5.5; confirmed by the founder on 2026-10-06 as the
answer to open question 2 in Section 23)

## Context

Design partners are regulated (healthcare payer, logistics with payroll data). Their security
reviews ask where raw identifiers and PHI go and who can reach them. Hosting a multi-tenant SaaS
that receives raw data would make the review and data-residency story hard and would make the
vendor a PHI processor from day one.

## Decision

v1 runs entirely inside the customer's environment: Docker Compose for pilots, Helm on the
customer's Kubernetes for production. No inbound vendor access, no phone-home, upgrades pulled
from a signed registry. The system is still split into an **edge** (the only component that sees
raw values: connectors, parsing, redaction, tokenization, reveal vault) and a **core** (tokenized
data only: storage, correlation, detection, API, UI). `tenant_id` is present on every table and
every event from day one even though v1 has one tenant.

## Alternatives

- Hosted core with on-prem edge: simpler fleet operations and upgrades, but the core would
  receive tokenized customer data off-site in v1, which complicates the first security reviews.
  The edge/core split keeps this open for v2 without redesign.
- Fully hosted SaaS: rejected for v1 because the vendor would hold PHI.

## Consequences

Upgrades, support and fleet visibility are harder; a support bundle command and self-alerts
compensate. Core code must never accept raw identifier values except the search box query, which
is forwarded to the edge tokenize endpoint and never stored. Tests enforce the boundary (leak
test, Section 18.3).
