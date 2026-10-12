import type { Auth, ChangeStatus, ConnectionStatus, IncidentStatus, RiskClass, VersionStatus } from "./types";

export type Tone = "ok" | "warn" | "bad" | "idle" | "neutral";

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const hasZone = /(?:Z|[+-]\d\d:?\d\d)$/i;

/** The registry serializes naive UTC timestamps without a zone; read those as UTC, not local time. */
export function parseDate(iso: string): Date {
  return new Date(hasZone.test(iso) ? iso : `${iso}Z`);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = parseDate(iso);
  return Number.isNaN(date.getTime()) ? iso : dateTime.format(date);
}

export function formatRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const time = parseDate(iso).getTime();
  if (Number.isNaN(time)) return iso;
  const seconds = Math.round((time - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 60) return relative.format(seconds, "second");
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour");
  return relative.format(Math.round(seconds / 86400), "day");
}

export function formatDuration(startIso: string, endIso: string): string {
  const ms = parseDate(endIso).getTime() - parseDate(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function pluralize(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function shortId(id: string | null | undefined, length = 8): string {
  return id ? id.slice(0, length) : "";
}

export const versionStatus: Record<VersionStatus, { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "neutral" },
  verified: { label: "Verified", tone: "ok" },
  published: { label: "Published", tone: "ok" },
  superseded: { label: "Superseded", tone: "idle" },
  rejected: { label: "Rejected", tone: "bad" },
};

export const connectionStatus: Record<ConnectionStatus, { label: string; tone: Tone }> = {
  pending_consent: { label: "Waiting for consent", tone: "warn" },
  active: { label: "Active", tone: "ok" },
  needs_reconsent: { label: "Needs re-consent", tone: "bad" },
  revoked: { label: "Revoked", tone: "idle" },
};

export function authLabel(auth: Auth | undefined): string {
  switch (auth?.type) {
    case "api_key":
      return "API key";
    case "bearer":
      return "Bearer token";
    case "basic":
      return "Basic auth";
    case "oauth2":
      return "OAuth 2.0";
    case "none":
      return "No auth";
    default:
      return "Unknown auth";
  }
}

/** Secret names a connection must supply for this auth scheme (mirrors IntegrationManifest.secret_refs). */
export function secretRefs(auth: Auth): string[] {
  switch (auth.type) {
    case "api_key":
    case "bearer":
      return [auth.secret_ref];
    case "basic":
      return [auth.username_secret_ref, auth.password_secret_ref].filter((ref): ref is string => !!ref);
    case "oauth2":
      return ["access_token"];
    default:
      return [];
  }
}

const eventLabels: Record<string, string> = {
  connection_created: "Connection created",
  consent_started: "Consent started",
  consent_granted: "Consent granted",
  consent_failed: "Consent failed",
  token_refreshed: "Token refreshed",
  refresh_failed: "Refresh failed",
  reconsent_required: "Re-consent required",
  revoked: "Revoked",
};

export function eventLabel(event: string): string {
  return eventLabels[event] ?? event.replaceAll("_", " ");
}

export function eventTone(event: string): Tone {
  if (event === "consent_granted" || event === "token_refreshed") return "ok";
  if (event === "consent_failed" || event === "refresh_failed" || event === "reconsent_required") return "bad";
  if (event === "revoked") return "idle";
  return "neutral";
}

export function notificationTone(kind: string): Tone {
  return kind === "reconsent_required" || kind.includes("fail") ? "bad" : "neutral";
}

export const incidentStatus: Record<IncidentStatus, { label: string; tone: Tone }> = {
  open: { label: "Open", tone: "warn" },
  triaged: { label: "Triaged", tone: "warn" },
  in_repair: { label: "In repair", tone: "warn" },
  needs_human: { label: "Needs a person", tone: "bad" },
  repair_failed: { label: "Repair failed", tone: "bad" },
  resolved: { label: "Resolved", tone: "ok" },
  dismissed: { label: "Dismissed", tone: "idle" },
};

export const changeStatus: Record<ChangeStatus, { label: string; tone: Tone }> = {
  pending: { label: "Waiting for approval", tone: "warn" },
  approved: { label: "Approved", tone: "ok" },
  rejected: { label: "Rejected", tone: "idle" },
  canary: { label: "In canary", tone: "warn" },
  promoted: { label: "Promoted", tone: "ok" },
  aborted: { label: "Canary aborted", tone: "bad" },
  failed: { label: "Failed", tone: "bad" },
  rolled_back: { label: "Rolled back", tone: "idle" },
};

export const riskTone: Record<RiskClass, Tone> = { low: "ok", medium: "warn", high: "bad" };

/** "schema_violation" reads as "Schema violation". */
export function kindLabel(kind: string): string {
  const words = kind.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
