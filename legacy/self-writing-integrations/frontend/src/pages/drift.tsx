import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { ChangesTable, IncidentsTable } from "@/components/drift-tables";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Checkbox } from "@/components/ui/field";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage } from "@/lib/api";
import { pluralize } from "@/lib/format";
import { useHealth, useIntegrationManifests } from "@/lib/hooks";
import type { WorkerTick } from "@/lib/types";

function describeTick(tick: WorkerTick): string {
  const parts = [
    tick.triaged.length ? `${tick.triaged.length} triaged` : "",
    tick.repaired.length ? `${tick.repaired.length} repaired` : "",
    tick.canaries_started.length ? `${tick.canaries_started.length} canaries started` : "",
    tick.promoted.length ? `${tick.promoted.length} promoted` : "",
    tick.aborted.length ? `${tick.aborted.length} aborted` : "",
    tick.waiting.length ? `${tick.waiting.length} waiting` : "",
  ].filter(Boolean);
  const errors = Object.keys(tick.errors).length;
  return (parts.length ? parts.join(", ") : "Nothing to do") + (errors ? `, ${pluralize(errors, "error")}` : "");
}

export function DriftPage() {
  const [search, setSearch] = useSearchParams();
  const tab = search.get("tab") === "changes" ? "changes" : "incidents";
  const [activeOnly, setActiveOnly] = useState(true);
  const queryClient = useQueryClient();
  const health = useHealth();
  const manifests = useIntegrationManifests();
  const incidents = useQuery({
    queryKey: ["incidents", activeOnly],
    queryFn: () => api.drift.incidents({ active: activeOnly }),
  });
  const changes = useQuery({ queryKey: ["changes"], queryFn: () => api.changes.list() });
  const worlds = useQuery({
    queryKey: ["drift-worlds"],
    queryFn: api.mock.driftWorlds,
    enabled: health.data?.mode === "mock",
  });

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["incidents"] }),
      queryClient.invalidateQueries({ queryKey: ["changes"] }),
      queryClient.invalidateQueries({ queryKey: ["integrations"] }),
      queryClient.invalidateQueries({ queryKey: ["versions"] }),
    ]);
  };
  const tick = useMutation({
    mutationFn: api.drift.tick,
    onSuccess: async (result) => {
      await invalidate();
      toast.message("Worker tick finished", { description: describeTick(result) });
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const clearDrift = useMutation({
    mutationFn: (integration: string) => api.mock.clearDrift(integration),
    onSuccess: async (_, integration) => {
      await queryClient.invalidateQueries({ queryKey: ["drift-worlds"] });
      toast.success(`The mock provider for ${manifests.current[integration]?.manifest.display_name ?? integration} behaves normally again`);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  const displayName = (integration: string) => manifests.current[integration]?.manifest.display_name ?? integration;
  const openIncidents = incidents.data?.filter((i) => i.status !== "resolved" && i.status !== "dismissed").length ?? 0;
  const waiting = changes.data?.filter((c) => c.status === "pending").length ?? 0;

  return (
    <>
      <PageHeader
        title="Drift"
        meta={
          <span>
            {pluralize(openIncidents, "open incident")}, {waiting} {waiting === 1 ? "change" : "changes"} waiting for
            approval
          </span>
        }
        actions={
          <Button
            disabled={tick.isPending}
            onClick={() => tick.mutate()}
            title="Runs one pass of the drift worker: triage, repair, canary checks, promotion"
          >
            {tick.isPending ? "Running" : "Run worker tick"}
          </Button>
        }
      />

      {worlds.data?.length ? (
        <Notice tone="warn" title="Simulated drift is active" className="mb-6">
          <ul className="space-y-1">
            {worlds.data.map((world) => (
              <li key={world.integration} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span>
                  {displayName(world.integration)} serves version {world.pinned_version} plus "{world.scenario.name}".
                </span>
                <Button size="sm" disabled={clearDrift.isPending} onClick={() => clearDrift.mutate(world.integration)}>
                  Restore the provider
                </Button>
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}

      <Tabs value={tab} onValueChange={(value) => setSearch({ tab: value }, { replace: true })}>
        <TabsList>
          <TabsTrigger value="incidents">Incidents{incidents.data ? ` (${incidents.data.length})` : ""}</TabsTrigger>
          <TabsTrigger value="changes">Change requests{changes.data ? ` (${changes.data.length})` : ""}</TabsTrigger>
        </TabsList>
        <TabsContent value="incidents" className="space-y-3">
          <Checkbox label="Active only" checked={activeOnly} onChange={(event) => setActiveOnly(event.target.checked)} />
          {incidents.error ? (
            <ErrorState error={incidents.error} retry={() => void incidents.refetch()} />
          ) : !incidents.data ? (
            <Loading />
          ) : incidents.data.length === 0 ? (
            <EmptyState title={activeOnly ? "No active incidents" : "No incidents"}>
              <p>
                The gateway validates every live response against the stored schema and watches status codes,
                headers and pagination. Anything off opens an incident here, grouped by integration, endpoint and
                kind.
              </p>
            </EmptyState>
          ) : (
            <IncidentsTable incidents={incidents.data} displayName={displayName} />
          )}
        </TabsContent>
        <TabsContent value="changes">
          {changes.error ? (
            <ErrorState error={changes.error} retry={() => void changes.refetch()} />
          ) : !changes.data ? (
            <Loading />
          ) : changes.data.length === 0 ? (
            <EmptyState title="No change requests">
              <p>
                A repair produces a candidate manifest version, verifies it against the API as it behaves now, and
                opens a change request. Nothing goes live until it is approved here or by a policy you set.
              </p>
            </EmptyState>
          ) : (
            <ChangesTable changes={changes.data} displayName={displayName} />
          )}
        </TabsContent>
      </Tabs>
    </>
  );
}
