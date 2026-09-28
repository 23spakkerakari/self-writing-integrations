import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { DecisionDialog } from "@/components/decision-dialog";
import { KeyValueFields, SecretFields, splitSecrets, type SecretSource } from "@/components/key-value-fields";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Facts } from "@/components/ui/facts";
import { Field, Select } from "@/components/ui/field";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Section } from "@/components/ui/section";
import { Mono, Status } from "@/components/ui/status";
import { VerificationReportView } from "@/components/verification-report";
import { api, errorMessage } from "@/lib/api";
import { connectionStatus, formatDateTime, incidentStatus, kindLabel, riskTone, secretRefs } from "@/lib/format";
import { currentVersion, useVersions } from "@/lib/hooks";
import type { Manifest, RepairOutcome } from "@/lib/types";

export function IncidentPage() {
  const { id = "" } = useParams();
  const incidentId = Number(id);
  const queryClient = useQueryClient();
  const incident = useQuery({
    queryKey: ["incident", incidentId],
    queryFn: () => api.drift.incident(incidentId),
    enabled: Number.isFinite(incidentId),
  });
  const versions = useVersions(incident.data?.integration);
  const [repairOpen, setRepairOpen] = useState(false);
  const [dismissOpen, setDismissOpen] = useState(false);
  const [outcome, setOutcome] = useState<RepairOutcome | null>(null);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["incident", incidentId] }),
      queryClient.invalidateQueries({ queryKey: ["incidents"] }),
      queryClient.invalidateQueries({ queryKey: ["changes"] }),
    ]);
  };
  const triage = useMutation({
    mutationFn: () => api.drift.triage(incidentId),
    onSuccess: async (result) => {
      await invalidate();
      toast.message(`Classified as ${result.triage.drift_class}, ${result.triage.risk_class} risk`, {
        description: result.triage.rationale,
      });
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const dismiss = useMutation({
    mutationFn: (decision: { actor: string; note: string }) => api.drift.dismiss(incidentId, decision),
    onSuccess: async () => {
      await invalidate();
      setDismissOpen(false);
      toast.success("Incident dismissed");
    },
  });

  if (incident.error) return <ErrorState error={incident.error} retry={() => void incident.refetch()} />;
  if (!incident.data) return <Loading />;

  const record = incident.data;
  const manifest = currentVersion(versions.data)?.manifest;
  const display = manifest?.display_name ?? record.integration;
  const status = incidentStatus[record.status];
  const closed = record.status === "resolved" || record.status === "dismissed";

  return (
    <>
      <PageHeader
        back={{ to: "/drift", label: "Drift" }}
        title={
          <>
            {kindLabel(record.kind)} on <span className="font-mono font-medium">{record.endpoint_id}</span>
          </>
        }
        meta={
          <>
            <Status tone={status.tone}>{status.label}</Status>
            <Link className="hover:underline" to={`/integrations/${record.integration}`}>
              {display} {record.version}
            </Link>
            <span>
              Seen {record.count} {record.count === 1 ? "time" : "times"}, first {formatDateTime(record.first_seen)}, last{" "}
              {formatDateTime(record.last_seen)}
            </span>
          </>
        }
        actions={
          closed ? null : (
            <>
              {!record.drift_class ? (
                <Button disabled={triage.isPending} onClick={() => triage.mutate()}>
                  {triage.isPending ? "Classifying" : "Triage"}
                </Button>
              ) : null}
              <Button onClick={() => setDismissOpen(true)}>Dismiss</Button>
              {record.change_request_id ? (
                <Button asChild variant="primary">
                  <Link to={`/changes/${record.change_request_id}`}>Review change {record.change_request_id}</Link>
                </Button>
              ) : (
                <Button variant="primary" disabled={record.repairable === false} onClick={() => setRepairOpen(true)}>
                  Repair
                </Button>
              )}
            </>
          )
        }
      />

      {record.repairable === false && !closed ? (
        <Notice tone="warn" className="mb-6" title="Triage does not think this can be repaired automatically">
          {record.triage_note || "A person needs to look at it."}
        </Notice>
      ) : null}
      {outcome?.error ? (
        <Notice tone="bad" className="mb-6" title="The repair did not produce a change request">
          {outcome.error}
        </Notice>
      ) : null}

      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="space-y-8">
          <Section title="What the gateway saw" description="One line per observed failure, most recent last">
            {record.samples.length ? (
              <ul className="divide-y divide-line rounded-lg border border-line bg-surface">
                {record.samples.map((sample, index) => (
                  <li key={index} className="px-4 py-2 font-mono text-xs break-all text-ink-2">
                    {sample}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-ink-3">No samples recorded.</p>
            )}
          </Section>
          {record.sample_body !== null && record.sample_body !== undefined ? (
            <Section title="Sample response body" description="The first page of the response that triggered the incident">
              <CodeBlock value={record.sample_body} maxHeight="28rem" />
            </Section>
          ) : null}
          {outcome?.report ? (
            <Section title="Repair verification" description={`Strategy ${outcome.strategy ?? "unknown"}, ${outcome.rounds} rounds`}>
              <VerificationReportView report={outcome.report} />
            </Section>
          ) : null}
        </div>
        <aside>
          <h2 className="mb-3 text-base font-medium">Triage</h2>
          <Facts
            items={[
              { label: "Class", value: record.drift_class },
              {
                label: "Risk",
                value: record.risk_class ? <Status tone={riskTone[record.risk_class]}>{record.risk_class}</Status> : null,
              },
              {
                label: "Repairable",
                value: record.repairable === null ? null : record.repairable ? "Yes" : "No",
              },
              { label: "Rationale", value: record.triage_note },
              { label: "Tenant", value: record.tenant_id ? <Mono>{record.tenant_id}</Mono> : null },
              { label: "HTTP status", value: record.status_code },
              {
                label: "Change request",
                value: record.change_request_id ? (
                  <Link className="hover:underline" to={`/changes/${record.change_request_id}`}>
                    Change {record.change_request_id}
                  </Link>
                ) : null,
              },
              { label: "Note", value: record.note },
              { label: "Resolved", value: record.resolved_at ? formatDateTime(record.resolved_at) : null },
            ]}
          />
        </aside>
      </div>

      {manifest ? (
        <RepairDialog
          open={repairOpen}
          onOpenChange={setRepairOpen}
          incidentId={incidentId}
          integration={record.integration}
          tenantId={record.tenant_id}
          manifest={manifest}
          onDone={async (result) => {
            setOutcome(result);
            await invalidate();
          }}
        />
      ) : null}
      <DecisionDialog
        open={dismissOpen}
        onOpenChange={setDismissOpen}
        title="Dismiss this incident?"
        description="Use this when the drift is expected or handled elsewhere. The incident stays in the history."
        confirmLabel="Dismiss incident"
        pending={dismiss.isPending}
        error={dismiss.error}
        onConfirm={(decision) => dismiss.mutate(decision)}
      />
    </>
  );
}

function RepairDialog({
  open,
  onOpenChange,
  incidentId,
  integration,
  tenantId,
  manifest,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  incidentId: number;
  integration: string;
  tenantId: string | null;
  manifest: Manifest;
  onDone: (outcome: RepairOutcome) => Promise<void>;
}) {
  const navigate = useNavigate();
  const isOAuth = manifest.auth.type === "oauth2";
  const refs = secretRefs(manifest.auth);
  const connections = useQuery({
    queryKey: ["connections", "all"],
    queryFn: () => api.connections.list(),
    enabled: open && isOAuth,
  });
  const candidates = (connections.data ?? []).filter((c) => c.integration_name === integration);
  const preferred = candidates.find((c) => c.tenant_id === tenantId && c.status === "active") ?? candidates.find((c) => c.status === "active");
  const [connectionId, setConnectionId] = useState("");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [secretSources, setSecretSources] = useState<Record<string, SecretSource>>({});
  const chosen = connectionId || preferred?.id || "";

  const repair = useMutation({
    mutationFn: () =>
      api.drift.repair(
        incidentId,
        isOAuth ? { connection_id: chosen } : { connection: { config, ...splitSecrets(refs, secretValues, secretSources) } },
      ),
    onSuccess: async (outcome) => {
      await onDone(outcome);
      onOpenChange(false);
      if (outcome.change_request) {
        toast.success(`Repair proposed as ${outcome.change_request.candidate_version}`, {
          description: outcome.strategy ?? undefined,
        });
        navigate(`/changes/${outcome.change_request.id}`);
      } else {
        toast.error("The repair did not produce a change request");
      }
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    repair.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Repair this incident</DialogTitle>
            <DialogDescription>
              Triage classifies the drift, a mechanical patch or the repair agent writes a candidate manifest, and
              the candidate is verified against the API as it behaves now. A change request opens for your
              approval. Nothing is published here.
            </DialogDescription>
          </DialogHeader>
          {isOAuth ? (
            <Field
              label="Verify through connection"
              htmlFor="repair-connection"
              hint="The candidate is exercised with this connection's vaulted tokens."
            >
              <Select id="repair-connection" value={chosen} onChange={(event) => setConnectionId(event.target.value)}>
                {candidates.length === 0 ? <option value="">No connections for {manifest.display_name}</option> : null}
                {candidates.map((c) => (
                  <option key={c.id} value={c.id} disabled={c.status !== "active"}>
                    {c.id.slice(0, 8)} ({connectionStatus[c.status].label.toLowerCase()})
                  </option>
                ))}
              </Select>
            </Field>
          ) : (
            <>
              {manifest.config_vars.length ? (
                <KeyValueFields
                  idPrefix="repair-config"
                  specs={manifest.config_vars.map((name) => ({ name, required: true, description: `Fills {${name}} in the base URL` }))}
                  values={config}
                  onChange={setConfig}
                />
              ) : null}
              <SecretFields
                idPrefix="repair-secret"
                refs={refs}
                values={secretValues}
                sources={secretSources}
                onChange={(values, sources) => {
                  setSecretValues(values);
                  setSecretSources(sources);
                }}
              />
            </>
          )}
          {repair.error ? (
            <Notice tone="bad" title="Repair could not run">
              {errorMessage(repair.error)}
            </Notice>
          ) : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={repair.isPending || (isOAuth && !chosen)}>
              {repair.isPending ? "Repairing, this can take a while" : "Run repair"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
