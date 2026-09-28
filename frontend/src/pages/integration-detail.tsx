import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { CallConsole } from "@/components/call-console";
import { DecisionDialog } from "@/components/decision-dialog";
import { ChangesTable, IncidentsTable } from "@/components/drift-tables";
import { SimulateDriftDialog } from "@/components/simulate-drift-dialog";
import { Button } from "@/components/ui/button";
import { Checkbox, Field, Select } from "@/components/ui/field";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Section } from "@/components/ui/section";
import { Mono, Status, Tag } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage } from "@/lib/api";
import { authLabel, formatDateTime, riskTone, versionStatus } from "@/lib/format";
import { currentVersion, useHealth, useVersions } from "@/lib/hooks";
import { useTenant } from "@/lib/tenant";
import type { Decision, Manifest, RiskClass, VersionRecord } from "@/lib/types";

export function IntegrationPage() {
  const { name = "" } = useParams();
  const versions = useVersions(name);
  const health = useHealth();
  const queryClient = useQueryClient();
  const { tenantId } = useTenant();
  const [callVersion, setCallVersion] = useState<string | null>(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [driftOpen, setDriftOpen] = useState(false);
  const worlds = useQuery({
    queryKey: ["drift-worlds"],
    queryFn: api.mock.driftWorlds,
    enabled: health.data?.mode === "mock",
  });
  const rollback = useMutation({
    mutationFn: (decision: Decision) => api.rollback(name, decision),
    onSuccess: async (record) => {
      await queryClient.invalidateQueries({ queryKey: ["versions", name] });
      await queryClient.invalidateQueries({ queryKey: ["integrations"] });
      await queryClient.invalidateQueries({ queryKey: ["changes"] });
      setRollbackOpen(false);
      toast.success(`Rolled back. ${record.version} is the published version again.`);
    },
  });
  const clearDrift = useMutation({
    mutationFn: () => api.mock.clearDrift(name),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["drift-worlds"] });
      toast.success("The mock provider behaves normally again");
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  if (versions.error) return <ErrorState error={versions.error} retry={() => void versions.refetch()} />;
  if (!versions.data) return <Loading />;
  const current = currentVersion(versions.data);
  if (!current) return <ErrorState error={new Error(`No versions stored for ${name}.`)} />;

  const manifest = current.manifest;
  const published = versions.data.find((v) => v.status === "published");
  const canRollBack = !!published && versions.data.some((v) => v.status === "superseded");
  const newestFirst = [...versions.data].reverse();
  const callTarget = versions.data.find((v) => v.version === callVersion) ?? published ?? current;
  const world = worlds.data?.find((w) => w.integration === name);
  const isMock = health.data?.mode === "mock";

  return (
    <>
      <PageHeader
        back={{ to: "/integrations", label: "Integrations" }}
        title={manifest.display_name}
        meta={
          <>
            <Mono>{name}</Mono>
            <span>{authLabel(manifest.auth)}</span>
            {published ? (
              <Status tone="ok">Published {published.version}</Status>
            ) : (
              <Status tone="idle">Nothing published</Status>
            )}
            {manifest.description ? <span className="basis-full">{manifest.description}</span> : null}
          </>
        }
        actions={
          <>
            {isMock ? (
              <Button variant="ghost" onClick={() => setDriftOpen(true)}>
                Simulate drift
              </Button>
            ) : null}
            {canRollBack ? (
              <Button variant="danger" onClick={() => setRollbackOpen(true)}>
                Roll back
              </Button>
            ) : null}
            <Button asChild>
              <Link to={`/integrations/new?tab=synthesize&name=${name}`}>Synthesize a new version</Link>
            </Button>
            <Button asChild>
              <Link to="/integrations/new?tab=import">Import a version</Link>
            </Button>
          </>
        }
      />

      {world ? (
        <Notice tone="warn" className="mb-6" title="Simulated drift is active">
          <span>
            The mock provider serves version {world.pinned_version} plus "{world.scenario.name}".{" "}
          </span>
          <Button size="sm" className="ml-2" disabled={clearDrift.isPending} onClick={() => clearDrift.mutate()}>
            Restore the provider
          </Button>
        </Notice>
      ) : null}

      <Tabs defaultValue="versions">
        <TabsList>
          <TabsTrigger value="versions">Versions</TabsTrigger>
          <TabsTrigger value="endpoints">Endpoints</TabsTrigger>
          <TabsTrigger value="drift">Drift</TabsTrigger>
          <TabsTrigger value="policy">Approval policy</TabsTrigger>
          <TabsTrigger value="call">Call</TabsTrigger>
        </TabsList>
        <TabsContent value="versions">
          <VersionsTable name={name} versions={newestFirst} />
        </TabsContent>
        <TabsContent value="endpoints">
          <p className="mb-3 text-xs text-ink-3">
            From version {current.version}
            {published ? ", the published one." : ", the newest, since nothing is published yet."}
          </p>
          <EndpointsTable manifest={manifest} />
        </TabsContent>
        <TabsContent value="drift">
          <IntegrationDrift name={name} display={manifest.display_name} />
        </TabsContent>
        <TabsContent value="policy">
          <PolicyPanel name={name} />
        </TabsContent>
        <TabsContent value="call">
          {manifest.auth.type === "oauth2" ? (
            <Notice>
              OAuth integrations are called through a tenant connection, so tokens come from the vault and never
              from the caller.{" "}
              <Link className="underline" to="/connections">
                Open connections
              </Link>
              .
            </Notice>
          ) : (
            <div className="space-y-5">
              {isMock ? (
                <Notice tone="warn">
                  The gateway is in mock mode. Calls go to a mock built from this manifest's response schemas,
                  not to the real API.
                </Notice>
              ) : null}
              <CallConsole
                key={callTarget.version}
                manifest={callTarget.manifest}
                mode="direct"
                controls={
                  <Field
                    label="Version"
                    htmlFor="call-version"
                    hint="The published version by default. Pick another to try a draft."
                  >
                    <Select
                      id="call-version"
                      value={callTarget.version}
                      onChange={(event) => setCallVersion(event.target.value)}
                    >
                      {newestFirst.map((v) => (
                        <option key={v.version} value={v.version}>
                          {v.version} ({versionStatus[v.status].label.toLowerCase()})
                        </option>
                      ))}
                    </Select>
                  </Field>
                }
                onCall={(input) =>
                  api.integrations.call(name, {
                    endpoint_id: input.endpoint_id,
                    params: input.params,
                    paginate: input.paginate,
                    version: callTarget.version,
                    connection: {
                      tenant_id: tenantId ?? "default",
                      config: input.config,
                      secrets: input.secrets,
                      secret_env: input.secret_env,
                    },
                  })
                }
              />
            </div>
          )}
        </TabsContent>
      </Tabs>

      <DecisionDialog
        open={rollbackOpen}
        onOpenChange={setRollbackOpen}
        title={`Roll back ${manifest.display_name}?`}
        description="The previously published version becomes the published one again, and the change request that promoted the current version is marked rolled back."
        confirmLabel="Roll back"
        variant="danger"
        pending={rollback.isPending}
        error={rollback.error}
        onConfirm={(decision) => rollback.mutate(decision)}
      />
      {isMock ? <SimulateDriftDialog open={driftOpen} onOpenChange={setDriftOpen} name={name} manifest={manifest} /> : null}
    </>
  );
}

function VersionsTable({ name, versions }: { name: string; versions: VersionRecord[] }) {
  const queryClient = useQueryClient();
  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["versions", name] });
    await queryClient.invalidateQueries({ queryKey: ["integrations"] });
  };
  const verify = useMutation({
    mutationFn: (version: string) => api.integrations.verify(name, version, { mode: "mock" }),
    onSuccess: async (report, version) => {
      await invalidate();
      const message = `${name} ${version} ${report.passed ? "passed" : "failed"} verification on mock`;
      if (report.passed) toast.success(message);
      else toast.error(message);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const publish = useMutation({
    mutationFn: (version: string) => api.integrations.publish(name, version),
    onSuccess: async (record) => {
      await invalidate();
      toast.success(`Published ${name} ${record.version}`);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  return (
    <Table>
      <THead>
        <TR>
          <TH>Version</TH>
          <TH>Status</TH>
          <TH>Provenance</TH>
          <TH>Verification</TH>
          <TH>Created</TH>
          <TH>Published</TH>
          <TH className="text-right">Actions</TH>
        </TR>
      </THead>
      <TBody>
        {versions.map((v) => {
          const status = versionStatus[v.status];
          return (
            <TR key={v.version}>
              <TD>
                <Link
                  to={`/integrations/${name}/versions/${v.version}`}
                  className="font-mono text-[13px] font-medium hover:underline"
                >
                  {v.version}
                </Link>
              </TD>
              <TD>
                <Status tone={status.tone}>{status.label}</Status>
              </TD>
              <TD className="text-ink-2">{v.provenance}</TD>
              <TD>
                {v.verification ? (
                  <Status tone={v.verification.passed ? "ok" : "bad"}>
                    {v.verification.passed ? "Passed" : "Failed"} on {v.verification.mode}
                  </Status>
                ) : (
                  <span className="text-ink-3">Not run</span>
                )}
              </TD>
              <TD className="whitespace-nowrap text-ink-2">{formatDateTime(v.created_at)}</TD>
              <TD className="whitespace-nowrap text-ink-2">
                {v.published_at ? formatDateTime(v.published_at) : <span className="text-ink-3">No</span>}
              </TD>
              <TD>
                <div className="flex justify-end gap-1.5">
                  {v.status !== "published" && v.status !== "superseded" ? (
                    <Button size="sm" disabled={verify.isPending} onClick={() => verify.mutate(v.version)}>
                      Verify on mock
                    </Button>
                  ) : null}
                  {v.status === "verified" ? (
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={publish.isPending}
                      onClick={() => publish.mutate(v.version)}
                    >
                      Publish
                    </Button>
                  ) : null}
                </div>
              </TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}

function EndpointsTable({ manifest }: { manifest: Manifest }) {
  return (
    <Table>
      <THead>
        <TR>
          <TH>Endpoint</TH>
          <TH>Request</TH>
          <TH>Pagination</TH>
          <TH>Scopes</TH>
          <TH>Maps to</TH>
          <TH>Description</TH>
        </TR>
      </THead>
      <TBody>
        {manifest.endpoints.map((endpoint) => {
          const mapping = manifest.mappings.find((m) => m.endpoint_id === endpoint.id);
          return (
            <TR key={endpoint.id}>
              <TD>
                <Mono className="font-medium">{endpoint.id}</Mono>
              </TD>
              <TD>
                <span className="font-mono text-xs text-ink-2">
                  {endpoint.method} {endpoint.path}
                </span>
              </TD>
              <TD className="text-ink-2">
                {endpoint.pagination.style === "none" ? "None" : endpoint.pagination.style}
              </TD>
              <TD>
                {endpoint.scopes.length ? (
                  <span className="flex flex-wrap gap-1">
                    {endpoint.scopes.map((scope) => (
                      <Tag key={scope}>{scope}</Tag>
                    ))}
                  </span>
                ) : (
                  <span className="text-ink-3">None</span>
                )}
              </TD>
              <TD>{mapping ? mapping.canonical_object : <span className="text-ink-3">Passthrough</span>}</TD>
              <TD className="text-ink-2">{endpoint.description}</TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}

function IntegrationDrift({ name, display }: { name: string; display: string }) {
  const incidents = useQuery({ queryKey: ["incidents", "integration", name], queryFn: () => api.drift.incidents({ integration: name }) });
  const changes = useQuery({ queryKey: ["changes", "integration", name], queryFn: () => api.changes.list({ integration: name }) });
  const label = () => display;
  return (
    <div className="space-y-8">
      <Section title="Incidents" description="Every drift the gateway has noticed on this integration, open or closed">
        {incidents.error ? (
          <ErrorState error={incidents.error} retry={() => void incidents.refetch()} />
        ) : !incidents.data ? (
          <Loading />
        ) : incidents.data.length === 0 ? (
          <p className="text-sm text-ink-3">No drift has been observed on {display}.</p>
        ) : (
          <IncidentsTable incidents={incidents.data} displayName={label} />
        )}
      </Section>
      <Section title="Change requests" description="Repairs proposed for this integration and what happened to them">
        {changes.error ? (
          <ErrorState error={changes.error} retry={() => void changes.refetch()} />
        ) : !changes.data ? (
          <Loading />
        ) : changes.data.length === 0 ? (
          <p className="text-sm text-ink-3">No repairs have been proposed for {display}.</p>
        ) : (
          <ChangesTable changes={changes.data} displayName={label} />
        )}
      </Section>
    </div>
  );
}

const riskRows: { risk: RiskClass; examples: string }[] = [
  { risk: "low", examples: "An optional response field appears, or a rate limit changes." },
  { risk: "medium", examples: "Behaviour changes that do not touch identity or auth, such as pagination or retry policy." },
  { risk: "high", examples: "Anything touching auth, removed fields, or canonical mappings." },
];

function PolicyPanel({ name }: { name: string }) {
  const queryClient = useQueryClient();
  const policy = useQuery({ queryKey: ["policy", name], queryFn: () => api.policy.get(name) });
  const update = useMutation({
    mutationFn: (body: { risk_class: RiskClass; auto_approve: boolean }) => api.policy.set(name, body),
    onSuccess: (result, body) => {
      queryClient.setQueryData(["policy", name], result);
      toast.success(`${body.risk_class} risk repairs are now ${body.auto_approve ? "approved automatically" : "held for a person"}`);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  if (policy.error) return <ErrorState error={policy.error} retry={() => void policy.refetch()} />;
  if (!policy.data) return <Loading />;

  return (
    <div className="max-w-3xl space-y-4">
      <p className="text-sm leading-6 text-ink-2">
        Every repair waits for a person by default. Once you trust the pipeline for a class of change, let the
        drift worker approve it on its own: it still verifies the candidate and runs a canary before promoting.
      </p>
      <Table>
        <THead>
          <TR>
            <TH>Risk class</TH>
            <TH>For example</TH>
            <TH>Approval</TH>
          </TR>
        </THead>
        <TBody>
          {riskRows.map((row) => (
            <TR key={row.risk}>
              <TD>
                <Status tone={riskTone[row.risk]}>{row.risk}</Status>
              </TD>
              <TD className="text-ink-2">{row.examples}</TD>
              <TD>
                <Checkbox
                  label="Approve automatically"
                  checked={policy.data.auto_approve[row.risk]}
                  disabled={update.isPending}
                  onChange={(event) => update.mutate({ risk_class: row.risk, auto_approve: event.target.checked })}
                />
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}
