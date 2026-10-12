import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { toast } from "sonner";
import { KeyValueFields, SecretFields, splitSecrets, type SecretSource } from "@/components/key-value-fields";
import { ManifestView } from "@/components/manifest-view";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Field, Input } from "@/components/ui/field";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Status } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { VerificationReportView } from "@/components/verification-report";
import { api, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, secretRefs, versionStatus } from "@/lib/format";
import { useVersions } from "@/lib/hooks";
import { useTenant } from "@/lib/tenant";
import type { Manifest, VersionRecord, VersionStatus } from "@/lib/types";

export function VersionPage() {
  const { name = "", version = "" } = useParams();
  const versions = useVersions(name);
  const queryClient = useQueryClient();
  const [liveOpen, setLiveOpen] = useState(false);

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["versions", name] });
    await queryClient.invalidateQueries({ queryKey: ["integrations"] });
  };
  const verifyMock = useMutation({
    mutationFn: () => api.integrations.verify(name, version, { mode: "mock" }),
    onSuccess: async (report) => {
      await invalidate();
      if (report.passed) toast.success(`${name} ${version} passed verification on mock`);
      else toast.error(`${name} ${version} failed verification on mock`);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const publish = useMutation({
    mutationFn: () => api.integrations.publish(name, version),
    onSuccess: async () => {
      await invalidate();
      toast.success(`Published ${name} ${version}`);
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  if (versions.error) return <ErrorState error={versions.error} retry={() => void versions.refetch()} />;
  if (!versions.data) return <Loading />;
  const record = versions.data.find((v) => v.version === version);
  if (!record) return <ErrorState error={new Error(`${name} has no version ${version}.`)} />;

  const status = versionStatus[record.status];
  const publishBlock = publishBlocker(record.status);
  const superseder = versions.data.find((v) => v.status === "published" && v.version !== version);

  return (
    <>
      <PageHeader
        back={{ to: `/integrations/${name}`, label: record.manifest.display_name }}
        title={
          <>
            {record.manifest.display_name} <span className="font-mono font-medium">{version}</span>
          </>
        }
        block={[
          { label: "Status", value: <Status tone={status.tone}>{status.label}</Status> },
          { label: "Provenance", value: record.provenance },
          { label: "Created", value: formatDateTime(record.created_at) },
          { label: "Published", value: record.published_at ? formatDateTime(record.published_at) : null },
        ]}
        actions={
          <>
            {record.status !== "published" && record.status !== "superseded" ? (
              <>
                <Button disabled={verifyMock.isPending} onClick={() => verifyMock.mutate()}>
                  {verifyMock.isPending ? "Verifying" : "Verify on mock"}
                </Button>
                <Button onClick={() => setLiveOpen(true)}>Verify live</Button>
              </>
            ) : null}
            <Button
              variant="primary"
              disabled={!!publishBlock || publish.isPending}
              title={publishBlock ?? undefined}
              onClick={() => publish.mutate()}
            >
              Publish {version}
            </Button>
          </>
        }
      />

      <Lifecycle status={record.status} />
      {record.status === "superseded" && superseder ? (
        <Notice className="mt-4">
          Superseded by {superseder.version}, which is the published version now.
        </Notice>
      ) : null}
      {publishBlock && record.status !== "published" && record.status !== "superseded" ? (
        <p className="mt-3 text-xs text-ink-3">{publishBlock}</p>
      ) : null}

      <Tabs defaultValue="report" className="mt-8">
        <TabsList>
          <TabsTrigger value="report">Verification</TabsTrigger>
          <TabsTrigger value="manifest">Manifest</TabsTrigger>
          <TabsTrigger value="json">Raw JSON</TabsTrigger>
        </TabsList>
        <TabsContent value="report">
          {record.verification ? (
            <VerificationReportView report={record.verification} />
          ) : (
            <EmptyState
              title="Not verified yet"
              actions={
                <Button variant="primary" disabled={verifyMock.isPending} onClick={() => verifyMock.mutate()}>
                  Verify on mock
                </Button>
              }
            >
              <p>
                Verification calls every endpoint against a mock built from the manifest's own response schemas
                and grades status, validation, mapping and identity completeness. Only verified versions can be
                published.
              </p>
            </EmptyState>
          )}
        </TabsContent>
        <TabsContent value="manifest">
          <ManifestView manifest={record.manifest} />
        </TabsContent>
        <TabsContent value="json">
          <CodeBlock value={record.manifest} maxHeight="70vh" />
        </TabsContent>
      </Tabs>

      <LiveVerifyDialog
        open={liveOpen}
        onOpenChange={setLiveOpen}
        record={record}
        onVerified={async () => {
          await invalidate();
        }}
      />
    </>
  );
}

function publishBlocker(status: VersionStatus): string | null {
  switch (status) {
    case "draft":
      return "Verify this version before publishing it.";
    case "rejected":
      return "Verification failed. Fix the manifest, store it as a new version and verify again.";
    case "published":
      return "This version is already published.";
    case "superseded":
      return "A newer version has been published since.";
    default:
      return null;
  }
}

const steps = ["Draft", "Verified", "Published"] as const;

/** The real sequence a version moves through, so a stepper is honest here. */
function Lifecycle({ status }: { status: VersionStatus }) {
  const reached = status === "draft" ? 0 : status === "verified" || status === "rejected" ? 1 : 2;
  return (
    <ol className="flex items-center gap-3 text-sm">
      {steps.map((step, index) => {
        const rejectedHere = status === "rejected" && index === 1;
        const done = index < reached;
        const current = index === reached;
        const label = rejectedHere ? "Rejected" : step;
        return (
          <li key={step} className="flex items-center gap-3">
            <span
              className={cn(
                "flex items-center gap-2",
                current ? "font-medium text-ink" : done ? "text-ink-2" : "text-ink-3",
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "inline-block size-2 rounded-full",
                  rejectedHere ? "bg-bad" : done || current ? "bg-ink" : "border border-line-strong",
                )}
              />
              {label}
            </span>
            {index < steps.length - 1 ? (
              <span aria-hidden className={cn("h-px w-8", done ? "bg-ink" : "bg-line-strong")} />
            ) : null}
          </li>
        );
      })}
      {status === "superseded" ? <li className="text-ink-3">then superseded</li> : null}
    </ol>
  );
}

function LiveVerifyDialog({
  open,
  onOpenChange,
  record,
  onVerified,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  record: VersionRecord;
  onVerified: () => Promise<void>;
}) {
  const manifest: Manifest = record.manifest;
  const { tenantId } = useTenant();
  const refs = secretRefs(manifest.auth);
  const [tenant, setTenant] = useState(tenantId ?? "default");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [secretSources, setSecretSources] = useState<Record<string, SecretSource>>({});

  const verify = useMutation({
    mutationFn: () =>
      api.integrations.verify(record.name, record.version, {
        mode: "live",
        connection: { tenant_id: tenant, config, ...splitSecrets(refs, secretValues, secretSources) },
      }),
    onSuccess: async (report) => {
      await onVerified();
      if (report.passed) toast.success(`${record.name} ${record.version} passed live verification`);
      else toast.error(`${record.name} ${record.version} failed live verification`);
      onOpenChange(false);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    verify.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Verify {record.version} against the live API</DialogTitle>
            <DialogDescription>
              Runs the same checks as mock verification, but with real credentials against the real API. The
              report is stored on the version either way.
            </DialogDescription>
          </DialogHeader>
          {manifest.auth.type === "oauth2" ? (
            <Notice tone="warn">
              This integration uses OAuth. Live verification needs an access token; supply one below, or verify
              through an active connection once one exists.
            </Notice>
          ) : null}
          <Field label="Tenant id" htmlFor="live-tenant" hint="Recorded with the gateway calls">
            <Input id="live-tenant" value={tenant} onChange={(event) => setTenant(event.target.value)} />
          </Field>
          {manifest.config_vars.length ? (
            <KeyValueFields
              idPrefix="live-config"
              specs={manifest.config_vars.map((name) => ({
                name,
                required: true,
                description: `Fills {${name}} in the base URL`,
              }))}
              values={config}
              onChange={setConfig}
            />
          ) : null}
          <SecretFields
            idPrefix="live-secret"
            refs={refs}
            values={secretValues}
            sources={secretSources}
            onChange={(values, sources) => {
              setSecretValues(values);
              setSecretSources(sources);
            }}
          />
          {verify.error ? (
            <Notice tone="bad" title="Verification could not run">
              {errorMessage(verify.error)}
            </Notice>
          ) : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={verify.isPending}>
              {verify.isPending ? "Verifying" : "Verify live"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
