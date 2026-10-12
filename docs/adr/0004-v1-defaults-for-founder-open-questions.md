# ADR 0004: v1 defaults for the open founder questions

Status: accepted, 2026-10-06 (spec Section 23; answered by the founder on 2026-10-06)

## Context

Section 23 lists questions only the founder can answer. Several of them set configuration
defaults that code needs from M0 onward.

## Decision

| Question | Decision for v1 |
| --- | --- |
| 1. Product name | Open. Codename `carto` for repo, package and service names. |
| 2. Deployment model | Fully in the customer's environment (ADR 0002). |
| 3. LLM assist | Off by default. Provider pluggable through `llm-gateway`; the default provider when a customer enables it is Claude through the Anthropic API, or their own Bedrock or Vertex account. Never required. |
| 4. First partner data path | Offline bundle first (`carto-edge analyze`), then install. |
| 5. Retention defaults | Events and identifiers 30 days, transaction membership 90 days, hourly aggregates and alerts 13 months, audit log 1 year, reveal vault follows events (Section 14.10). |
| 6. Source availability | `carto-edge` is intended to be source-available to customers for security review. No open-source license is attached; the repository stays proprietary until counsel reviews the customer source-access terms. |
| 7. Counsel review | Open (BAA needs, pilot agreements, data processing terms). Not a build blocker. |
| 8. Pricing | Open. Outside the build spec. |

## Alternatives

Each row's alternative is listed in Section 23; the recommended answers were accepted unchanged.

## Consequences

Configuration schemas carry these defaults. `docs/install/` must describe how to change retention
and how to enable the LLM. Marketing and docs must not claim compliance before counsel review
(Section 14.13).
