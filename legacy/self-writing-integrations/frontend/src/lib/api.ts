import type {
  ApprovalPolicy,
  AuthEvent,
  CallResult,
  CanaryReport,
  CanonicalReference,
  ChangeRequest,
  ChangeStatus,
  ConnectionRecord,
  ConnectionSpec,
  ConsentRequest,
  Decision,
  DriftIncident,
  DriftScenario,
  DriftWorldInfo,
  Health,
  IncidentStatus,
  IntegrationSummary,
  Notification,
  OAuthApp,
  PipelineResult,
  RepairOutcome,
  RiskClass,
  Tenant,
  Triage,
  VerificationReport,
  VersionRecord,
  WorkerTick,
} from "./types";

interface PydanticIssue {
  loc?: (string | number)[];
  msg?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(describeDetail(detail) || `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function issueText(issue: PydanticIssue): string {
  const loc = (issue.loc ?? []).filter((part) => part !== "body").join(" / ");
  return loc ? `${loc}: ${issue.msg ?? ""}` : (issue.msg ?? JSON.stringify(issue));
}

export function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((issue) => issueText(issue as PydanticIssue)).join("\n");
  if (detail && typeof detail === "object" && "msg" in detail) return issueText(detail as PydanticIssue);
  return detail == null ? "" : JSON.stringify(detail);
}

/** Field-level issues from a 422, one line each, for rendering as a list. */
export function validationIssues(error: unknown): string[] {
  if (error instanceof ApiError && Array.isArray(error.detail)) {
    return error.detail.map((issue) => issueText(issue as PydanticIssue));
  }
  return [];
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export const apiBase = ((import.meta.env.VITE_API_BASE as string | undefined) ?? "/api").replace(/\/$/, "");

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiBase + path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The API is not reachable. Start it from backend/ with: uvicorn app.main:app --reload");
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    throw new ApiError(response.status, detail ?? response.statusText);
  }
  return data as T;
}

const get = <T>(path: string) => request<T>("GET", path);
const post = <T>(path: string, body?: unknown) => request<T>("POST", path, body);
const put = <T>(path: string, body?: unknown) => request<T>("PUT", path, body);
const del = <T>(path: string) => request<T>("DELETE", path);

function query(params: Record<string, string | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined) search.set(key, String(value));
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => get<Health>("/health"),
  canonical: () => get<CanonicalReference>("/canonical"),

  integrations: {
    list: () => get<IntegrationSummary[]>("/integrations"),
    versions: (name: string) => get<VersionRecord[]>(`/integrations/${name}/versions`),
    version: (name: string, version: string) => get<VersionRecord>(`/integrations/${name}/versions/${version}`),
    import: (manifest: Record<string, unknown>, provenance = "manual") =>
      post<VersionRecord>("/integrations/import", { manifest, provenance }),
    synthesize: (body: { name: string; spec_text: string; max_rounds: number }) =>
      post<PipelineResult>("/integrations/synthesize", body),
    verify: (name: string, version: string, body: { mode: "mock" } | { mode: "live"; connection: ConnectionSpec }) =>
      post<VerificationReport>(`/integrations/${name}/versions/${version}/verify`, body),
    publish: (name: string, version: string) => post<VersionRecord>(`/integrations/${name}/versions/${version}/publish`),
    call: (
      name: string,
      body: { endpoint_id: string; params: Record<string, unknown>; paginate: boolean; version?: string; connection: ConnectionSpec },
    ) => post<CallResult>(`/integrations/${name}/call`, body),
  },

  tenants: {
    list: () => get<Tenant[]>("/tenants"),
    create: (name: string) => post<Tenant>("/tenants", { name }),
  },

  apps: {
    register: (body: { integration_name: string; client_id: string; client_secret: string; redirect_uri?: string }) =>
      post<OAuthApp>("/oauth/apps", body),
  },

  connections: {
    list: (tenantId?: string) => get<ConnectionRecord[]>(`/connections${query({ tenant_id: tenantId })}`),
    get: (id: string) => get<ConnectionRecord>(`/connections/${id}`),
    create: (body: { tenant_id: string; integration_name: string; config: Record<string, string> }) =>
      post<ConnectionRecord>("/connections", body),
    consent: (id: string, endpointIds?: string[]) =>
      post<ConsentRequest>(`/connections/${id}/consent`, endpointIds ? { endpoint_ids: endpointIds } : undefined),
    refresh: (id: string) => post<ConnectionRecord>(`/connections/${id}/refresh`),
    revoke: (id: string) => post<ConnectionRecord>(`/connections/${id}/revoke`),
    audit: (id: string) => get<AuthEvent[]>(`/connections/${id}/audit`),
    call: (id: string, body: { endpoint_id: string; params: Record<string, unknown>; paginate: boolean }) =>
      post<CallResult>(`/connections/${id}/call`, body),
  },

  oauth: {
    callback: (state: string, code: string) => get<ConnectionRecord>(`/oauth/callback${query({ state, code })}`),
  },

  audit: (tenantId: string) => get<AuthEvent[]>(`/audit${query({ tenant_id: tenantId })}`),

  notifications: {
    list: (tenantId: string, unreadOnly = false) =>
      get<Notification[]>(`/notifications${query({ tenant_id: tenantId, unread_only: unreadOnly || undefined })}`),
    markRead: (id: number) => post<void>(`/notifications/${id}/read`),
  },

  drift: {
    incidents: (params: { integration?: string; status?: IncidentStatus; active?: boolean } = {}) =>
      get<DriftIncident[]>(
        `/drift/incidents${query({ integration: params.integration, status: params.status, active: params.active || undefined })}`,
      ),
    incident: (id: number) => get<DriftIncident>(`/drift/incidents/${id}`),
    triage: (id: number) => post<{ incident: DriftIncident; triage: Triage }>(`/drift/incidents/${id}/triage`),
    repair: (id: number, body: { connection_id?: string; connection?: ConnectionSpec }) =>
      post<RepairOutcome>(`/drift/incidents/${id}/repair`, body),
    dismiss: (id: number, body: Decision) => post<DriftIncident>(`/drift/incidents/${id}/dismiss`, body),
    tick: () => post<WorkerTick>("/drift/tick"),
  },

  changes: {
    list: (params: { integration?: string; status?: ChangeStatus } = {}) =>
      get<ChangeRequest[]>(`/changes${query({ integration: params.integration, status: params.status })}`),
    get: (id: number) => get<ChangeRequest>(`/changes/${id}`),
    approve: (id: number, body: Decision) => post<ChangeRequest>(`/changes/${id}/approve`, body),
    reject: (id: number, body: Decision) => post<ChangeRequest>(`/changes/${id}/reject`, body),
    startCanary: (id: number, fraction?: number) =>
      post<ChangeRequest>(`/changes/${id}/canary`, fraction ? { fraction } : undefined),
    canaryReport: (id: number) => get<CanaryReport>(`/changes/${id}/canary`),
    promote: (id: number, body: Decision & { force?: boolean }) => post<ChangeRequest>(`/changes/${id}/promote`, body),
    abort: (id: number, body: Decision) => post<ChangeRequest>(`/changes/${id}/abort`, body),
  },

  policy: {
    get: (name: string) => get<ApprovalPolicy>(`/integrations/${name}/approval-policy`),
    set: (name: string, body: { risk_class: RiskClass; auto_approve: boolean; actor?: string }) =>
      put<ApprovalPolicy>(`/integrations/${name}/approval-policy`, body),
  },

  rollback: (name: string, body: Decision) => post<VersionRecord>(`/integrations/${name}/rollback`, body),

  mock: {
    authorize: (authorizeUrl: string) =>
      post<{ redirect_url: string; state: string; code: string }>("/mock/authorize", { authorize_url: authorizeUrl }),
    revokeAtProvider: (integration: string) => post<{ status: string }>(`/mock/revoke-at-provider/${integration}`),
    driftWorlds: () => get<DriftWorldInfo[]>("/mock/drift"),
    injectDrift: (integration: string, scenario: DriftScenario) =>
      post<DriftWorldInfo>(`/mock/drift/${integration}`, { scenario }),
    clearDrift: (integration: string) => del<{ status: string }>(`/mock/drift/${integration}`),
  },
};
