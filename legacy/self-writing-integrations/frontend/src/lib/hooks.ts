import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { VersionRecord } from "./types";

export function useHealth() {
  return useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 30_000, retry: false });
}

export function useIntegrations() {
  return useQuery({ queryKey: ["integrations"], queryFn: api.integrations.list });
}

export function useVersions(name: string | undefined) {
  return useQuery({ queryKey: ["versions", name], queryFn: () => api.integrations.versions(name!), enabled: !!name });
}

/** The version that represents an integration today: the published one, else the newest. */
export function currentVersion(versions: VersionRecord[] | undefined): VersionRecord | undefined {
  if (!versions?.length) return undefined;
  return versions.find((v) => v.status === "published") ?? versions[versions.length - 1];
}

/** Every integration with its current version, so lists can show auth type and endpoints. */
export function useIntegrationManifests() {
  const list = useIntegrations();
  const names = list.data?.map((i) => i.name) ?? [];
  const versionQueries = useQueries({
    queries: names.map((name) => ({ queryKey: ["versions", name], queryFn: () => api.integrations.versions(name) })),
  });
  const current: Record<string, VersionRecord | undefined> = {};
  names.forEach((name, index) => {
    current[name] = currentVersion(versionQueries[index]?.data);
  });
  return {
    integrations: list.data ?? [],
    current,
    isLoading: list.isLoading || versionQueries.some((q) => q.isLoading),
    error: list.error,
    refetch: list.refetch,
  };
}

export function useConnections(tenantId: string | null) {
  return useQuery({
    queryKey: ["connections", tenantId],
    queryFn: () => api.connections.list(tenantId!),
    enabled: !!tenantId,
  });
}

export function useConnection(id: string | undefined) {
  return useQuery({ queryKey: ["connection", id], queryFn: () => api.connections.get(id!), enabled: !!id });
}
