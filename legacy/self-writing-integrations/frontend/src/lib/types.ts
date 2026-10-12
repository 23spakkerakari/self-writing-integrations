// Mirrors the Pydantic DTOs in backend/app. Keep field names identical to the API.

export type VersionStatus = "draft" | "verified" | "published" | "superseded" | "rejected";
export type ConnectionStatus = "pending_consent" | "active" | "needs_reconsent" | "revoked";
export type DriftKind = "schema_violation" | "unexpected_status" | "malformed_body" | "transport_error";
export type GatewayMode = "mock" | "live";

export type JsonSchema = {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  description?: string;
  enum?: unknown[];
  anyOf?: JsonSchema[];
  format?: string;
  title?: string;
  default?: unknown;
  [key: string]: unknown;
};

export interface IntegrationSummary {
  name: string;
  display_name: string;
  published_version: string | null;
  latest_version: string | null;
  version_count: number;
}

export interface Parameter {
  name: string;
  location: "path" | "query" | "header";
  required: boolean;
  description: string;
}

export interface Pagination {
  style: "none" | "page" | "offset" | "cursor";
  page_param: string;
  size_param: string | null;
  page_size: number;
  offset_param: string;
  limit_param: string;
  cursor_param: string;
  next_cursor_path: string | null;
}

export interface Endpoint {
  id: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  description: string;
  parameters: Parameter[];
  default_query: Record<string, string>;
  default_headers: Record<string, string>;
  response_schema: JsonSchema | null;
  items_path: string | null;
  pagination: Pagination;
  scopes: string[];
}

export interface FieldMap {
  target: string;
  source: string | null;
  transform: string | null;
  args: Record<string, unknown>;
}

export interface Mapping {
  endpoint_id: string;
  canonical_object: string;
  fields: FieldMap[];
}

export type Auth =
  | { type: "none" }
  | { type: "api_key"; location: "header" | "query"; name: string; prefix: string; secret_ref: string }
  | { type: "bearer"; secret_ref: string }
  | {
      type: "basic";
      username_secret_ref: string | null;
      username_literal: string | null;
      password_secret_ref: string | null;
      password_literal: string | null;
    }
  | {
      type: "oauth2";
      flow: "authorization_code";
      authorization_url: string;
      token_url: string;
      scopes: string[];
      scope_separator: string;
      pkce: boolean;
      token_auth_method: "client_secret_post" | "client_secret_basic";
      refresh_leeway_seconds: number;
      extra_authorization_params: Record<string, string>;
    };

export interface Manifest {
  name: string;
  version: string;
  display_name: string;
  description: string;
  base_url: string;
  config_vars: string[];
  auth: Auth;
  default_headers: Record<string, string>;
  rate_limit: { requests_per_second: number; burst: number };
  retry: { max_attempts: number; backoff_seconds: number; retry_on_status: number[] };
  endpoints: Endpoint[];
  mappings: Mapping[];
}

export interface EndpointCheck {
  endpoint_id: string;
  passed: boolean;
  status_code: number | null;
  pages: number;
  records: number;
  canonical: number;
  errors: string[];
  warnings: string[];
}

export interface VerificationReport {
  integration: string;
  version: string;
  mode: GatewayMode;
  passed: boolean;
  checks: EndpointCheck[];
  started_at: string;
  finished_at: string;
}

export interface VersionRecord {
  name: string;
  version: string;
  status: VersionStatus;
  provenance: string;
  manifest: Manifest;
  verification: VerificationReport | null;
  created_at: string;
  published_at: string | null;
}

export interface DriftEvent {
  integration: string;
  version: string;
  endpoint_id: string;
  kind: DriftKind;
  detail: string;
  observed_at: string;
}

export interface CallResult {
  endpoint_id: string;
  ok: boolean;
  status_code: number | null;
  url: string;
  pages: number;
  raw_first_page: unknown;
  records: unknown[];
  canonical_object: string | null;
  canonical: Record<string, unknown>[];
  validation_errors: string[];
  mapping_errors: string[];
  drift_events: DriftEvent[];
}

export interface PipelineResult {
  record: VersionRecord;
  report: VerificationReport;
  rounds: number;
  model_attempts: number;
}

export interface Tenant {
  id: string;
  name: string;
  created_at: string;
}

export interface OAuthApp {
  integration_name: string;
  client_id: string;
  redirect_uri: string;
  created_at: string;
}

export interface ConnectionRecord {
  id: string;
  tenant_id: string;
  integration_name: string;
  config: Record<string, unknown>;
  status: ConnectionStatus;
  granted_scopes: string[];
  token_expires_at: string | null;
  last_refreshed_at: string | null;
  refresh_count: number;
  created_at: string;
  updated_at: string;
}

export interface ConsentRequest {
  connection_id: string;
  integration_name: string;
  authorize_url: string;
  state: string;
  scopes: string[];
  expires_at: string;
}

export interface AuthEvent {
  id: number;
  tenant_id: string;
  connection_id: string | null;
  event: string;
  scopes: string[];
  actor: string;
  detail: string;
  at: string;
}

export interface Notification {
  id: number;
  tenant_id: string;
  connection_id: string | null;
  kind: string;
  message: string;
  read: boolean;
  created_at: string;
}

export interface Health {
  status: string;
  mode: GatewayMode;
  version: string;
}

export interface CanonicalReference {
  objects: Record<string, JsonSchema>;
  transforms: Record<string, string>;
}

export interface ConnectionSpec {
  tenant_id?: string;
  config?: Record<string, string>;
  secret_env?: Record<string, string>;
  secrets?: Record<string, string>;
}

// --- drift and repair (milestone 3) -------------------------------------------------------

export type IncidentStatus =
  | "open"
  | "triaged"
  | "in_repair"
  | "needs_human"
  | "repair_failed"
  | "resolved"
  | "dismissed";
export type DriftClass = "schema" | "behavioral" | "auth" | "deprecation" | "semantic" | "transient" | "cosmetic";
export type RiskClass = "low" | "medium" | "high";
export type ChangeStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "canary"
  | "promoted"
  | "aborted"
  | "failed"
  | "rolled_back";

export interface Triage {
  drift_class: DriftClass;
  risk_class: RiskClass;
  repairable: boolean;
  rationale: string;
}

export interface DriftIncident {
  id: number;
  integration: string;
  endpoint_id: string;
  kind: string;
  status: IncidentStatus;
  version: string;
  tenant_id: string | null;
  status_code: number | null;
  first_seen: string;
  last_seen: string;
  count: number;
  samples: string[];
  sample_body: unknown;
  drift_class: DriftClass | null;
  risk_class: RiskClass | null;
  repairable: boolean | null;
  triage_note: string;
  change_request_id: number | null;
  note: string;
  resolved_at: string | null;
}

export interface DiffOp {
  op: "add" | "remove" | "replace";
  path: string;
  from?: unknown;
  to?: unknown;
}

export interface ArmStats {
  version: string;
  calls: number;
  failures: number;
  validation_errors: number;
  drift_events: number;
}

export interface CanaryReport {
  change_request_id: number;
  fraction: number;
  min_calls: number;
  base: ArmStats;
  candidate: ArmStats;
  verdict: "insufficient" | "pass" | "fail";
  reason: string;
}

export interface ChangeRequest {
  id: number;
  integration: string;
  incident_id: number | null;
  base_version: string;
  candidate_version: string;
  drift_class: DriftClass;
  risk_class: RiskClass;
  strategy: string;
  diff: DiffOp[];
  verification: VerificationReport | null;
  verified: boolean;
  status: ChangeStatus;
  auto_approved: boolean;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string;
  canary_fraction: number | null;
  canary_started_at: string | null;
  canary: CanaryReport | null;
  promoted_at: string | null;
}

export interface ApprovalPolicy {
  integration: string;
  auto_approve: Record<RiskClass, boolean>;
}

export interface RepairOutcome {
  incident: DriftIncident;
  triage: Triage;
  change_request: ChangeRequest | null;
  report: VerificationReport | null;
  strategy: string | null;
  rounds: number;
  error: string | null;
}

export interface WorkerTick {
  triaged: number[];
  repaired: number[];
  canaries_started: number[];
  promoted: number[];
  aborted: number[];
  waiting: number[];
  errors: Record<string, string>;
}

export interface DriftMutation {
  type: string;
  [key: string]: unknown;
}

export interface DriftScenario {
  name: string;
  mutations: DriftMutation[];
}

export interface DriftWorldInfo {
  integration: string;
  pinned_version: string;
  scenario: DriftScenario;
}

export interface Decision {
  actor: string;
  note: string;
}
