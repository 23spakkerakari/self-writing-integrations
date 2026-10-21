# ADR 0059: The data layer, business rule reconciliation and the hosted deployment enter the spec as v2

Status: proposed, 2026-10-21 (spec Sections 1.4, 5.5, 10.1, 10.2, 22.2 to 22.4, 23; amends ADR
0002 for v2; the founder's direction of 2026-10-20 and 2026-10-21)

## Context

The founder asked for three things on 2026-10-20: a hosted data layer that lets an underwriter
open one case and see the selected fields from every system that holds the party, with
identity reconciled across systems; a way for partners to reconcile conflicting business rules
and keep data in step; and detection of partial failures, stale data and missed events. The
founder answered the questions about the data layer over two rounds and has no partner answers
yet for the other two. The spec is the source of truth, so the direction had to be written down
before anything else moved.

## Decision

- The data layer is **v2**, alongside Fix and connect, written as spec Section 22.2 with its
  own invariants D1 to D7. It sits on the map: the map's connectors and change feeds are its
  discovery and its refresh triggers. Values are shown one case at a time, from a per-tenant
  data plane at the edge trust level, never from core.
- Business rule reconciliation is **v2**, Section 22.3: attribute families and value
  crosswalks through the review queue, a rule registry, conflict detection on tokens where
  values are high-cardinality, and synchronization as drift reporting with write-back reserved
  for the actuator. v1 keeps the groundwork (attributes on events, a reserved `consistency`
  expectation kind, generic review and audit).
- Partial failures, stale data and missed events are **v1, milestone M4**: Section 10.1 gains
  the `completeness` and `follow_up` expectation kinds and reserves `consistency`; Section 10.2
  gains partial-failure detection by attribute slice and the record-level staleness
  definition. Each stands on an assumption named in Section 23.
- **Hosting**: core in our cloud, multi-tenant; the data plane per tenant, isolated, in our
  cloud by default and in the customer's network on request (Section 22.4). ADR 0002 stays in
  force for v1; whether the v1 design partners move is Section 23 question 13.
- Three assumptions the founder has not confirmed are written as such in the spec: overrides
  live in the layer and never change the source (the founder's wording implied a write and a
  later overwrite, which contradict each other and invariant 1); the supervisor is a new
  role; the sandbox is an optional pre-publication review per tenant.
- ADR numbers for the M3 plan's decisions start at 0060.

## Alternatives

- A core-side curated store with selected fields in clear: simpler, but it changes the security
  story both partners are buying and it blocks the hosted core.
- Treating the data layer as v1 scope: it would reshape M3 to M6 around a product the first
  partners did not ask for, and the identity resolution it needs is M2's output, which the gate
  has not yet accepted.
- Writing overrides into sources now: a write path to customer systems, forbidden by
  invariant 1 until the actuator of Section 22.1 exists with its approvals.

## Consequences

- No milestone plan changes until the founder returns the partner's answers; the M4 plan
  picks up the new expectation kinds and the slice detection when it is written.
- The hosted model makes the M6 cloud KMS and secret manager wrappers (ADR 0012, 0013) and a
  SOC 2 program prerequisites for the first hosted customer.
- This ADR moves to accepted when Section 23 questions 9 to 25 are answered and the assumptions
  are confirmed or replaced.
