# Self-Writing Integrations: Technical Architecture

An agentic platform that turns a target API into a working, tested integration, then keeps it
working as the API changes. Domain: people management (employees, departments). This
document records the agreed decisions and defines the architecture for each build milestone.

Status legend: **built** (in repo, tested), **designed** (this doc only), **open** (decision pending).

## 1. Decisions

| Question | Decision |
|---|---|
| Who consumes integrations | Both developers (monitoring integrations and data flows in a UI) and AI agents (acting on data flowing from one API into another). |
| Level of autonomy | Auto-patch with human approval. Fully autonomous promotion is deferred until the repair pipeline has earned trust; low-risk change classes may be unlocked per integration later. |
| API scope | Worst case: undocumented and internal APIs. Ingestion must work from captured traffic, not just specs. Active probing is restricted to safe HTTP methods. |
| Data model | A unified canonical model is required. Every integration maps raw records onto canonical objects. |
| Tenancy | Multi-tenant. OAuth consent screens are human-in-the-loop for compliance. Token refresh and re-consent detection are autonomous. Every auth event is audited. |
| Practice targets | No sandbox credentials yet. Practice on public, documented SaaS APIs (BambooHR first). Hide the real spec from the agent and score the inferred result against it. |
| Stack | Python 3.11, FastAPI, Pydantic v2, SQLAlchemy (SQLite locally, Postgres in deployment), httpx, jsonschema, Claude via the Anthropic SDK. React/TypeScript frontend. |
| Integrations as data | Manifest-first. Declarative manifests interpreted by a generic runtime, with sandboxed generated code only as an escape hatch. |

### What "full autonomous promotion" means

A repair goes live with no human in the loop. The only gates are automated: verification passes, a
canary slice of live traffic shows no error-rate regression, and the change is below a risk threshold.
We start with approval required for everything. Later, change classes are graded: adding an optional
response field or adjusting a rate limit is low risk; anything touching auth, removed fields, or canonical
mappings stays behind a human.

### What drift detection is

Noticing that the API you integrated has changed underneath you before users notice. Drift types, from
easiest to hardest to catch:

| Type | Example | Primary signal |
|---|---|---|
| Schema drift | Field renamed, removed, or changed type | Live response validation against the stored schema |
| Behavioral drift | Pagination style changes, rate limits tighten, error format changes | Pagination never terminates, 429 rate rises, non-JSON bodies |
| Auth drift | New required scopes, token endpoint moves, refresh tokens start expiring | 401/403 rate, refresh failures |
| Deprecation | Sunset headers, version retirement, 410 responses | Header inspection, status codes, changelog watch |
| Semantic drift | Same field name, different meaning; an enum gains a value | Mapping errors, unknown enum values, downstream data anomalies |

For undocumented APIs there is no spec to re-fetch, so live response validation is the primary signal.
That is why the runtime gateway validates every response instead of just executing calls.

## 2. The core loop

1. **Discover.** Ingest a docs URL, OpenAPI or GraphQL spec, Postman collection, or captured traffic. Normalize to an internal spec.
2. **Synthesize.** An agent produces a manifest: auth, endpoints, response schemas, pagination, retries, and canonical mappings.
3. **Verify.** Run the manifest against a spec-derived mock, then against a live test account when one exists. Failures feed back into synthesis.
4. **Publish.** Store the version in the registry and expose it through the runtime.
5. **Watch.** Validate live responses, re-fetch and diff specs, watch error rates, headers, and changelogs. Emit drift events.
6. **Repair.** Triage the drift, patch the manifest, verify, canary, promote through an approval gate.

## 3. Manifest-first design

A manifest is data: base URL, auth scheme, endpoints with JSON Schemas, pagination policy, rate and retry
policies, and field-level mappings to canonical objects. A generic runtime interprets it.

Consequences:

- The agent maintains integrations by patching structured data. Diffs are small and reviewable.
- Drift maps onto manifest fields: a renamed response field is a schema patch plus a mapping patch.
- Verification is schema-driven. The mock server, the validator, and the test harness are all generated from the manifest.
- Transforms are a closed registry of named functions. The agent can only choose from them, so no arbitrary code ever enters a manifest.
- Generated code is the escape hatch for APIs that do not fit (custom signing, multi-step handshakes) and runs sandboxed behind a narrow interface.

## 4. Canonical people model

| Object | Fields |
|---|---|
| Employee | source_integration, source_id, first_name, last_name, display_name, work_email, personal_email, job_title, department, division, location, manager_source_id, manager_display_name, hire_date, termination_date, employment_status (active, on_leave, terminated, unknown), work_phone, mobile_phone |
| Department | source_integration, source_id, name, parent_source_id |

Rules: `source_id` must always be mapped. An Employee must carry at least one identity field (display name,
work email, first name, or last name). Renaming a canonical field is a breaking change for every mapping in
the registry, so the model grows by addition only.

## 5. Milestones

### Milestone 1: Manifest and runtime (built)

**Goal.** Prove synthesize, verify, publish, and call for one API-key integration with one canonical object.

**Architecture.**

| Component | Module | Responsibility |
|---|---|---|
| Manifest schema | `backend/app/manifest/schema.py` | Pydantic model with cross-checks: unique endpoint ids, declared path params, known canonical targets, known transforms, base URL variables listed in config vars. Extra keys are rejected. |
| Canonical model | `backend/app/canonical/people.py` | Employee and Department, the mapping contract. |
| Transforms | `backend/app/runtime/transforms.py` | Closed registry: to_date, enum_map, concat, first_non_null, const, split_take, and coercions. |
| Runtime gateway | `backend/app/runtime/gateway.py` | Base URL templating from connection config, auth injection (API key, bearer, basic), token-bucket rate limiting, retries with backoff and Retry-After, page/offset/cursor pagination, JSON Schema validation of every response, mapping to canonical objects. Emits DriftEvents for schema violations, unexpected statuses, malformed bodies, and transport failures. |
| Secrets | `backend/app/runtime/secrets.py` | Provider interface. Environment-backed for now; the vault replaces it in milestone 2. Only the gateway resolves secrets. |
| Mock server | `backend/app/verification/mock_server.py` | In-process httpx transport generated from the manifest: name-aware example bodies from response schemas, auth enforcement, pagination simulation, and a hook for mutating responses to simulate drift. |
| Verification harness | `backend/app/verification/harness.py` | Calls every endpoint (list endpoints first, harvesting ids for detail endpoints), grades status, validation, mapping, and identity completeness. Mock and live modes produce the same report shape. |
| Registry | `backend/app/registry/store.py` | Versioned, immutable manifests with status draft, verified, published, superseded, rejected. Publishing supersedes the previous published version. Stores verification reports and spec snapshots with content hashes. |
| Synthesis agent | `backend/app/synthesis/agent.py` | Claude (claude-opus-5) produces a manifest from a spec using structured output constrained to the manifest JSON Schema, falling back to free-form JSON if the schema is rejected. Validation errors are fed back for up to N attempts. The same entry point repairs a manifest given verification feedback. |
| Pipeline | `backend/app/synthesis/pipeline.py` | synthesize, verify on mock, feed failures back, repeat up to max rounds, store as verified or rejected. |
| Control plane API | `backend/app/api/routes.py` | Import, synthesize, list, verify (mock or live), publish, call. Connection secrets are passed as environment variable references and resolved server-side. |
| CLI | `backend/app/cli.py` | The same loop from the terminal. |

**Data model.**

```
integrations(name, display_name, created_at)
integration_versions(id, integration_name, version, status, manifest_json, spec_source,
                     provenance, verification_json, created_at, published_at)
spec_snapshots(id, integration_name, content, content_hash, fetched_at)
```

**Interfaces.**

```
GET  /health                                   GET  /canonical         GET /manifest-schema
GET  /integrations                             POST /integrations/import
POST /integrations/synthesize                  GET  /integrations/{name}/versions
GET  /integrations/{name}/versions/{v}         POST /integrations/{name}/versions/{v}/verify
POST /integrations/{name}/versions/{v}/publish POST /integrations/{name}/call
```

**Exit criteria.** Reference BambooHR manifest passes mock verification, publishes, and returns canonical
Employees through the gateway. Drift simulation (renamed field, 401, non-JSON body) fails verification with
the right event kind. Synthesis pipeline repairs a broken manifest from feedback. All met; 53 tests pass. The
live-Claude synthesis test runs when `ANTHROPIC_API_KEY` is set.

### Milestone 2: OAuth broker (built)

**Goal.** Multi-tenant connections against one OAuth API (Gusto's demo environment is the practice target).
Consent is human, everything after is autonomous, everything is audited.

**Architecture.**

| Component | Module | Responsibility |
|---|---|---|
| OAuth2 auth type | `backend/app/manifest/schema.py` | `oauth2` auth with authorization and token URLs, base scopes, per-endpoint scopes, PKCE, token endpoint auth method, refresh leeway. `required_scopes()` computes the minimal scope set for the endpoints in use. |
| Tenancy model | `backend/app/oauth/models.py` | Tenants, per-tenant wrapped data keys, registered OAuth apps, connections, credentials, audit events, notifications. Connection status: pending_consent, active, needs_reconsent, revoked. |
| Vault | `backend/app/oauth/vault.py` | Envelope encryption. A master key from `VAULT_MASTER_KEY` wraps one random data key per tenant. Credentials are AES-GCM encrypted with the tenant key and bound to tenant, connection, and kind, so a ciphertext cannot be replayed across tenants. Platform OAuth client secrets are encrypted with the master key. |
| Broker | `backend/app/oauth/broker.py` | Registers one OAuth app per integration. Creates connections. Builds the consent request with PKCE S256, a random state, and the minimal scope set. Completes consent by exchanging the code and storing tokens in the vault. Refreshes ahead of expiry, rotates refresh tokens, and on `invalid_grant` flips the connection to needs_reconsent, destroys its credentials, appends audit events, and notifies the tenant. Exposes a per-connection SecretsProvider that the gateway uses; on a 401 the gateway asks it to refresh once and retries. |
| Refresh scheduler | `backend/app/oauth/scheduler.py` | Ticks over connections inside their leeway window. Runs as a daemon thread with the API when `REFRESH_SCHEDULER=1`. Transient provider errors retry next tick. |
| Mock authorization server | `backend/app/verification/mock_oauth.py` | Plays the provider offline: simulated user approval, code exchange with PKCE verification, refresh token rotation, bearer validation for the mock API, and revocation and expiry controls for drift simulation. |
| Consent screen | `backend/app/api/oauth_routes.py` | Server-rendered page showing exactly which scopes will be requested, with the link to the provider. This is the human-in-the-loop step. The developer console renders the same screen at `/connections/{id}/consent` (`frontend/src/pages/consent.tsx`); the server-rendered page stays for API-only use. |
| Control plane | `backend/app/api/oauth_routes.py`, `mock_routes.py` | Tenants, app registration, connections, consent, callback, refresh, revoke, audit, notifications, and calling an integration through a connection so callers never handle credentials. Mock-mode routes simulate the user approving at the provider and revoking the app there. |

**Data model.**

```
tenants(id, name, created_at)
tenant_keys(tenant_id, wrapped_key, created_at)
oauth_apps(integration_name, client_id, client_secret_ciphertext, redirect_uri, created_at)
connections(id, tenant_id, integration_name, config_json, status, granted_scopes,
            pending_state, pending_verifier, pending_scopes, pending_expires_at,
            token_expires_at, last_refreshed_at, refresh_count, created_at, updated_at)
credentials(id, connection_id, kind, ciphertext, expires_at, rotated_at)
auth_events(id, tenant_id, connection_id, event, scopes, actor, detail, at)   -- append only
notifications(id, tenant_id, connection_id, kind, message, read, created_at)
```

Audit events: connection_created, consent_started, consent_granted, consent_failed, token_refreshed,
refresh_failed, reconsent_required, revoked.

**Interfaces.**

```
POST /tenants                                  POST /oauth/apps
POST /connections                              GET  /connections?tenant_id=
GET  /connections/{id}                         POST /connections/{id}/consent
GET  /connections/{id}/consent-page            GET  /oauth/callback?state=&code=
POST /connections/{id}/refresh                 POST /connections/{id}/revoke
GET  /connections/{id}/audit                   GET  /notifications?tenant_id=
POST /connections/{id}/call
POST /mock/authorize   POST /mock/revoke-at-provider/{integration}     (mock mode only)
```

**Exit criteria.** All met. A tenant completes consent once through the API and the connection becomes
active with the granted scopes recorded. A simulated week with hourly ticks produces 168 autonomous
refreshes, no re-consent, and no notifications. Revoking the app at the provider makes the next refresh
fail with `invalid_grant`, which flips the connection to needs_reconsent, destroys its credentials,
notifies the tenant, and stops further attempts. Re-consent restores the connection. The audit log
reconstructs every event per connection and per tenant. Vault tests prove tenant isolation and that a
wrong master key cannot unwrap anything. 79 tests pass.

**Not yet exercised.** The real Gusto demo environment has not been called. Doing so needs a registered
Gusto developer app: register it with `POST /oauth/apps`, set `PUBLIC_BASE_URL` to a reachable callback
host, and run with `GATEWAY_MODE=live`.

### Milestone 3: Drift and repair (built)

**Goal.** Detect every drift type against a drifted mock, patch it, verify, approve, canary, promote.

**Architecture.**

| Component | Where | What it does |
|---|---|---|
| Drift signals | `backend/app/runtime/gateway.py` | Every call emits DriftEvents: schema_violation, unexpected_status, malformed_body, transport_error, and now deprecation (Sunset/Deprecation headers, 410 Gone), pagination_runaway (max_pages exhausted while more is advertised) and mapping_error. Events carry the HTTP status. |
| Drift worlds | `backend/app/verification/drift_scenarios.py` | Middleware around the mock that pins the API to the published manifest plus a scenario: rename, remove or retype a field, a new enum value, a moved list key, a fixed status, a non-JSON body, a newly required header, a retired path with a successor, a Sunset notice, endless pagination. A candidate is verified against the world, never against a mock built from its own schema. `POST /mock/drift/{integration}` injects a scenario in mock mode. |
| Drift monitor | `backend/app/drift/monitor.py` | Folds events into `drift_incidents(integration, endpoint, kind, status, first_seen, last_seen, count, samples, sample_body)`: one incident per (integration, endpoint, kind) while unresolved. `check_spec` snapshots a re-fetched spec and opens a spec_changed incident carrying the diff. |
| Triage | `backend/app/drift/triage.py` | Deterministic rules, not a model: class (schema, behavioral, auth, deprecation, semantic, transient, cosmetic), risk (low, medium, high) and repairability. Risk gates the approval policy, so it must be reproducible. Anything touching auth, a mapped field or a retired endpoint is high. |
| Repair | `backend/app/drift/patches.py`, `backend/app/drift/repair.py` | A mechanical rename patch is tried first (schema, items_path, cursor path and mapping sources rewritten together). Otherwise the synthesis agent's repair mode receives the incident, its samples, the sample body, the latest spec snapshot and class-specific guidance; a verification failure feeds a second round. Every candidate is stored as a new version with provenance `repair:<strategy>:incident:<id>`. |
| Approval gate | `backend/app/drift/changes.py` | `change_requests(base_version, candidate_version, risk_class, diff, verification, status, decided_by, decided_at)`. The diff aligns lists by identity, so a reviewer reads `/mappings/[endpoint_id=list_employees]/fields/[target=display_name]/source`. `approval_policies` per integration and risk class, default off. |
| Canary and promote | `backend/app/drift/changes.py` | Calls without an explicit version take part in a running canary: a fraction goes to the candidate and every call's outcome is recorded per arm. Verdict: pass once the candidate has `CANARY_MIN_CALLS` calls, a failure rate under 10% and no worse than the base; fail otherwise. Promote publishes; abort keeps the base live. |
| Rollback | `backend/app/registry/store.py` | One step: the previous published version is re-published and the current one is marked rolled_back, so it can never be published again. The promoting change request is marked rolled_back and its incident goes back to a human. |
| Worker | `backend/app/drift/worker.py` | One tick triages open incidents, runs repairs, starts canaries for approved changes and judges running ones. In-process with `DRIFT_WORKER=1`, or on demand through `POST /drift/tick`. |
| Control plane | `backend/app/api/drift_routes.py` | `/drift/incidents`, `/drift/incidents/{id}/{triage,repair,dismiss}`, `/drift/spec-check/{name}`, `/drift/tick`, `/changes`, `/changes/{id}/{approve,reject,canary,promote,abort}`, `/integrations/{name}/approval-policy`, `/integrations/{name}/rollback`. CLI: `incidents`, `changes`, `approve`, `reject`, `promote`, `rollback`, `policy`. |

**Exit criteria, met.** Every drift type in the taxonomy is simulated by a drift world and detected as the
right kind. Repairable drift (renamed field, moved list key, new enum value, new auth header, retired path,
retyped field) is repaired and promoted through approval and canary; a Sunset notice and upstream failures
are routed to a human instead. Rollback restores the previous published version in one step. The suite is
142 tests; the live-agent repair test runs when `ANTHROPIC_API_KEY` is set.

**Decisions made while building.**

- Triage is rule-based because it decides how much supervision a repair gets; the model only proposes patches.
- A repair is verified against the API as it behaves now (a live connection, or the pinned drift world), never
  against a mock generated from the candidate's own schema.
- Incidents keep aggregating by (integration, endpoint, kind) while a human holds them, so a second change on
  the same endpoint adds evidence to the open incident rather than opening a parallel one.
- Changelog watchers and Sunset-driven migration planning stay manual for now; the notice opens an incident.

### Milestone 4: Traffic-first ingester (designed)

**Goal.** Build manifests for undocumented APIs from observed traffic, scored against hidden ground truth.

**Architecture.**

| Component | Responsibility |
|---|---|
| Capture inputs | HAR import, a recording proxy developers route through, SDK request logs. Stored as `traffic_samples(integration, method, url, request_headers, request_body, status, response_headers, response_body, captured_at)` with secrets redacted at ingest. |
| Endpoint clustering | Group samples by method and path pattern, inferring path variables from segments that vary. |
| Schema inference | Merge response bodies per endpoint into a JSON Schema with per-field confidence and sample counts. Fields seen in every sample become required; others optional and nullable. |
| Probing policy | Active probing only for GET and HEAD, only on endpoints already observed, rate limited, never on internal APIs without an explicit allowlist. |
| Benchmark | Run the ingester on a documented API with the spec hidden. Score inferred endpoints, fields, types, and required flags against the real spec. This is the acceptance metric. |

**Exit criteria.** Inferred BambooHR manifest from traffic alone reaches an agreed score against the
reference manifest, and the confidence scores correlate with correctness.

### Milestone 5: Flows (designed)

**Goal.** Move canonical objects from a source integration to a destination, with agents watching.

**Architecture.**

| Component | Responsibility |
|---|---|
| Flow definition | `flows(id, tenant_id, source_connection, source_endpoint, canonical_object, destination_connection, destination_endpoint, schedule, filter, status)`. Manifests gain write endpoints with request mappings (canonical to raw), the inverse of today's response mappings. |
| Flow runner | Scheduled or event-triggered: read from source through the gateway, map to canonical, dedupe by source_id, map to the destination's request shape, write through the gateway. `flow_runs(flow_id, started, finished, read, written, failed, errors)`. |
| Event bus | Flow and drift events published for agent subscribers. |
| Agent watchers | Agents subscribe to flow events and act: alert on failed writes, hold a flow when drift is open on its source, summarize a run. Their tool surface is the gateway and the registry, exposed as MCP servers. |
| Developer UI | Flow health, run history, per-record failures, drift incidents affecting a flow. |

**Exit criteria.** An Employee created in the source appears in the destination within one schedule tick.
A simulated drift on the source pauses the flow and opens a change request, and an agent reports it.

### Milestone 6: Codebase integrations (designed)

**Goal.** Drop the platform into any folder of a codebase. It reads the code, produces a manifest for the
API that code serves (routes, headers, request and response types, status codes), registers it as an
integration, and observes the service's live traffic through the same gateway, monitor and repair loop
as every other integration. Code becomes the third discovery source next to specs and captured traffic.

**Where it runs.**

| Place | What lives there |
|---|---|
| The target repo | A `.swi/` folder written by `codebase init`: the manifest draft, a lockfile pinning manifest version to commit hash, and a tap config listing the observed routes. One folder is one service, so a monorepo yields one integration per folder. |
| The platform | The registry, gateway, drift monitor and approval gate already built, plus a per-call ledger and a served-endpoint inspector. |
| CI | `codebase check` re-runs the extractor on every push and diffs against the published manifest. A diff opens a change request before any traffic shows it. |

**Architecture.**

| Component | Module | Responsibility |
|---|---|---|
| Manifest additions | `backend/app/manifest/schema.py` | `types`: named JSON Schemas referenced from endpoints as `#/$defs/<Name>`, so a type used by several routes is stored once and diffs once. Per endpoint: `direction` (`called`: the platform is the client, today's behaviour; `served`: the codebase is the server and the platform observes), `responses` keyed by status code for error shapes, `response_headers`. `resolve()` inlines type references for the validator and the mock. |
| Framework detection | `backend/app/codebase/detect.py` | Finds the frameworks in a folder from manifests (`pyproject.toml`, `package.json`, `go.mod`) and imports, and locates the application object. |
| Deterministic extractor | `backend/app/codebase/openapi.py`, `fastapi_app.py`, `ast_python.py` | Tier one. Where the framework can describe itself (FastAPI, NestJS, Spring) the extractor loads the app in a subprocess and converts its OpenAPI output to a served manifest with a deterministic converter. Where it cannot, a Python AST pass reads route decorators, handler signatures and return annotations. Exact types, no model calls. |
| Agent extractor | `backend/app/codebase/agent.py` | Tier two, for what tier one left blank: untyped handlers, dynamic routing, headers set by middleware, error shapes. The synthesis agent gets read-only code tools and is constrained to the manifest JSON Schema. Every inferred field carries a confidence and the source line that justified it. Source leaves the machine only here; an offline mode stops after tier one. |
| Lockfile | `backend/app/codebase/lock.py` | `.swi/lock.json`: integration name, manifest version, commit, folder, framework, extractor tier, content hash of the extraction. |
| Code snapshots | `backend/app/registry/store.py` | Extractions are stored like spec snapshots, with provenance `codebase:<repo>@<commit>` on the version they produced. |
| Call ledger | `backend/app/ledger/models.py`, `store.py` | One row per gateway call and per tapped exchange: tenant, integration, version, endpoint, direction, status, latency, validation outcome, drift kinds, trace and span ids. The observability promise in section 6 lands here. Bodies are not in the ledger; sampled bodies stay in `traffic_samples`. |
| Shared validation | `backend/app/runtime/validate.py` | Request and response validation factored out of the gateway so the inspector reuses it. |
| Inspector | `backend/app/runtime/inspector.py` | Matches a tapped exchange to a served endpoint by method and path template, validates the request against `request_schema` and the response against `response_schema` or `responses[status]`, emits DriftEvents and writes a ledger row. New kinds: `request_invalid` (client payload failed the schema), `handler_error` (served endpoint returned 5xx), `code_changed` (extraction differs from the published manifest). |
| Tap | `backend/app/tap/asgi.py`, `shipper.py`, `trace.py` | ASGI middleware installed with one line. Records each exchange, redacts with the capture rules, reads or mints a W3C `traceparent`, and ships batches to the platform asynchronously. Observe-only by default. `enforce` per route makes it inline: an invalid request is rejected with 422 before the handler runs. The recording transport forwards the trace id on outbound calls. |
| Flow graph | `backend/app/api/codebase_routes.py` | Edges derived from the ledger: an inbound record and the outbound records sharing its trace id. Milestone 5 flows become the case where the platform itself initiates the movement. |
| Benchmark | `backend/app/codebase/score.py` | Run the extractor on a repo whose framework can emit OpenAPI, hide that output, and score endpoints, parameters, types, required flags and status codes against it. This repo's own backend is the first target. |
| Control plane and CLI | `backend/app/api/codebase_routes.py`, `backend/app/cli.py` | Extraction upload, drift check, tap ingest, ledger queries, flow graph. CLI: `codebase init`, `codebase extract`, `codebase check`. |

**Data model.**

```
code_snapshots(id, integration, repo, commit, folder, framework, tier, report_json, content_hash, extracted_at)
call_records(id, tenant_id, integration, version, endpoint_id, direction, method, path, status, latency_ms,
             validation, drift_kinds, trace_id, span_id, parent_span_id, source, at)
```

**Interfaces.**

```
POST /codebase/extract            upload an extraction; stores a code snapshot and a draft version
POST /codebase/check              diff an extraction against the published manifest; opens code_changed
POST /tap/exchanges               batch of redacted exchanges from a tap
GET  /ledger/calls?integration=&endpoint_id=&trace_id=&since=
GET  /flows/graph?integration=
CLI: codebase init <folder>  codebase extract <folder>  codebase check <folder>
```

**How traffic reaches the platform.** Two directions, two answers. Outbound from the service is the
recording transport generalised into a thin SDK; those calls go through the gateway and are validated and
recorded there. Inbound to the service is the tap. It mirrors each exchange asynchronously, so the platform
is never an availability dependency and adds no latency; the inline `enforce` mode is opted into per route
where rejecting bad requests is worth the coupling. Both produce the same redacted exchange record, so the
discovery, inspection and monitoring code does not know which one produced it.

**Exit criteria.** The extractor run on this repo's own backend with the generated OpenAPI hidden reaches
an agreed score against it on endpoints, parameters, types and status codes. With the backend tapped under
the test client, every call appears in the ledger with a trace id, a malformed request opens a
`request_invalid` incident, an injected 500 opens a `handler_error` incident, and renaming a route in code
opens a `code_changed` change request before any traffic reflects it. A tapped inbound call that triggers an
outbound gateway call appears as one edge in the flow graph.

**Decisions.**

- Observe-only tap by default; inline enforcement is per route and opt-in. Being in the request path is a cost
  the service owner chooses, not a default.
- Code is a discovery source, not a separate pipeline. The extractor emits the same report object as the
  traffic ingester, and the two converge on one manifest. Code gives the intended contract, traffic gives the
  actual behaviour, and disagreement between them is a drift signal neither can produce alone.
- Triage stays deterministic. `request_invalid` is behavioural and low risk (a client fault, or a request
  schema that is too strict). `handler_error` is behavioural, medium, not repairable by a manifest patch.
  `code_changed` is schema drift, high when it touches a mapped field or a removed route, medium otherwise.
- Served traffic carries customer data. Bodies are sampled and redacted at the tap; the ledger holds metadata
  only; retention is a per-tenant setting.
- The deterministic tier must stand alone. Source code is sent to a model only in tier two, and only when the
  operator has not chosen offline mode.

**Build steps.** 1 manifest additions (types, direction, responses, response headers, `resolve()`).
2 call ledger and a gateway hook. 3 shared validation and the served-endpoint inspector. 4 tap middleware,
shipper, trace ids and the ingest route. 5 deterministic OpenAPI converter and FastAPI introspection, code
snapshots, `codebase extract`. 6 Python AST fallback and the scoring harness. 7 agent tier. 8 `codebase check`
and the `code_changed` gate. 9 flow graph and trace forwarding on the recording transport. 10 inline enforce
mode and the console pages.

## 6. Cross-cutting

- **Safety.** Generated code, when it exists, runs sandboxed with network access limited to the target API. Credentials never reach agents. Synthesis and repair runs have token budgets and attempt caps. Every change is a versioned artifact with rollback.
- **Approval defaults.** On for everything; loosened per integration and per risk class once trust is earned.
- **Observability.** Every gateway call and every tapped exchange records status, latency, validation outcome, trace id and drift events per tenant, integration, version, and endpoint in the call ledger (milestone 6). Bodies are sampled and redacted; the ledger holds metadata only.
- **Tenancy.** Connections, credentials, flows, and audit events are tenant-scoped from milestone 2 onward.

## 7. Repository layout

```
backend/
  app/
    canonical/     people.py            canonical objects
    manifest/      schema.py            manifest model and validation
    runtime/       gateway.py mapping.py transforms.py paths.py secrets.py validate.py inspector.py
    registry/      store.py             versioned storage
    oauth/         models.py vault.py broker.py scheduler.py   tenancy and OAuth broker
    synthesis/     agent.py pipeline.py Claude synthesis and repair loop
    drift/         models.py monitor.py triage.py patches.py repair.py changes.py worker.py diff.py
    verification/  mock_server.py mock_oauth.py harness.py drift_scenarios.py
    discovery/     models.py capture.py cluster.py infer.py semantic.py   traffic capture, clustering, inference, mapping proposals (milestone 4)
    codebase/      detect.py openapi.py fastapi_app.py ast_python.py agent.py lock.py score.py   code extraction (milestone 6, designed)
    tap/           asgi.py shipper.py trace.py   inbound tap for served APIs (milestone 6, designed)
    ledger/        models.py store.py            per-call ledger (milestone 6, designed)
    api/           routes.py oauth_routes.py drift_routes.py mock_routes.py codebase_routes.py common.py   FastAPI control plane
    db.py cli.py main.py config.py
  manifests/       bamboohr.yaml gusto.yaml   reference manifests
  specs/           bamboohr-employees.openapi.yaml   practice spec
  tests/
frontend/          developer console: Vite, React, TypeScript, Tailwind (npm run dev proxies /api to the backend)
  src/lib/         api.ts types.ts format.ts hooks.ts tenant.tsx   typed client, DTO mirrors, formatting, tenant context
  src/components/  ui/ shell/ call-console manifest-view verification-report drift-tables ...
  src/pages/       integrations, versions, connections, consent, drift, changes, activity, canonical
docs/              this document
```

## 8. Running milestone one

```
cd backend
pip install -e ".[dev]"
python -m pytest
python -m app.cli import manifests/bamboohr.yaml
python -m app.cli verify bamboohr 0.1.0
python -m app.cli publish bamboohr 0.1.0
set BAMBOOHR_API_KEY=your-key
python -m app.cli call bamboohr list_employees --config company_domain=acme --secret api_key=BAMBOOHR_API_KEY --mock
uvicorn app.main:app --reload
```

Drop `--mock` to hit the real BambooHR API once a key exists. Set `ANTHROPIC_API_KEY` and run
`python -m app.cli synthesize specs/bamboohr-employees.openapi.yaml --name bamboohr` to have the agent
build the manifest from the spec instead of importing the reference one.

## 9. Running milestone two (OAuth) against the mock provider

```
set GATEWAY_MODE=mock
set VAULT_MASTER_KEY=<output of: python -m app.cli vault-key>
uvicorn app.main:app --reload
```

Then, against `http://127.0.0.1:8000`:

1. Import, verify, and publish `manifests/gusto.yaml` through `/integrations/import`, `/verify`, `/publish`.
2. `POST /tenants {"name": "Acme"}` and `POST /oauth/apps {"integration_name": "gusto", "client_id": "...", "client_secret": "..."}`.
3. `POST /connections {"tenant_id": ..., "integration_name": "gusto"}` then open `/connections/{id}/consent-page`.
4. In mock mode, `POST /mock/authorize {"authorize_url": ...}` stands in for the tenant clicking Allow; call `GET /oauth/callback` with the returned state and code. Live mode redirects the browser there automatically.
5. `POST /connections/{id}/call {"endpoint_id": "list_employees", "params": {"company_uuid": "..."}}`.
6. `POST /mock/revoke-at-provider/gusto`, then `POST /connections/{id}/refresh` to see the needs_reconsent transition and `GET /notifications?tenant_id=...`.

Set `REFRESH_SCHEDULER=1` to run the background refresh loop inside the API process.

## 10. Running milestone three (drift and repair) against the mock provider

```
set GATEWAY_MODE=mock
uvicorn app.main:app --reload
```

1. Import, verify and publish `manifests/bamboohr.yaml` as in section 8.
2. Make the provider drift: `POST /mock/drift/bamboohr {"scenario": {"mutations": [{"type": "rename_field", "endpoint_id": "list_employees", "old": "displayName", "new": "display_name"}]}}`.
3. Call it: `POST /integrations/bamboohr/call {"endpoint_id": "list_employees", "connection": {"config": {"company_domain": "acme"}, "secrets": {"api_key": "x"}}}` returns `ok: false`, and `GET /drift/incidents` shows the incident.
4. Repair: `POST /drift/incidents/1/repair {}`. The mechanical patch is verified against the drifted mock and opens change request 1; `GET /changes/1` shows the diff and the verification report.
5. Approve and canary: `POST /changes/1/approve {"actor": "you"}`, `POST /changes/1/canary {"fraction": 0.5}`, make a few calls, then `GET /changes/1/canary`.
6. Promote: `POST /changes/1/promote {"actor": "you"}`. `GET /integrations` shows 0.1.1 published. `POST /integrations/bamboohr/rollback {}` restores 0.1.0.
7. Let the worker do steps 4 to 6: `PUT /integrations/bamboohr/approval-policy {"risk_class": "medium", "auto_approve": true}`, then `POST /drift/tick` after each batch of calls, or run with `DRIFT_WORKER=1`.

Other mutation types: `set_field` (a new enum value; needs the repair agent and `ANTHROPIC_API_KEY`),
`require_header`, `path_moved`, `retype_field`, `remove_field`, `wrap_items`, `status`, `non_json`,
`sunset`, `endless_pagination`. `DELETE /mock/drift/bamboohr` heals the provider. Environment:
`DRIFT_WORKER`, `DRIFT_INTERVAL_SECONDS`, `CANARY_FRACTION`, `CANARY_MIN_CALLS`, `REPAIR_MAX_ROUNDS`.
