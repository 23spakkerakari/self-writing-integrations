import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { DecisionDialog } from "@/components/decision-dialog";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Checkbox, Field, Input } from "@/components/ui/field";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Mono, Status } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { VerificationReportView } from "@/components/verification-report";
import { api, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { changeStatus, formatDateTime, riskTone } from "@/lib/format";
import { currentVersion, useVersions } from "@/lib/hooks";
import type { CanaryReport, ChangeStatus, Decision, DiffOp } from "@/lib/types";

export function ChangePage() {
  const { id = "" } = useParams();
  const changeId = Number(id);
  const queryClient = useQueryClient();
  const change = useQuery({
    queryKey: ["change", changeId],
    queryFn: () => api.changes.get(changeId),
    enabled: Number.isFinite(changeId),
    refetchInterval: (query) => (query.state.data?.status === "canary" ? 10_000 : false),
  });
  const versions = useVersions(change.data?.integration);
  const canary = useQuery({
    queryKey: ["canary", changeId],
    queryFn: () => api.changes.canaryReport(changeId),
    enabled: !!change.data?.canary_started_at,
    refetchInterval: change.data?.status === "canary" ? 10_000 : false,
  });
  const [dialog, setDialog] = useState<"approve" | "reject" | "abort" | "canary" | "promote" | null>(null);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["change", changeId] }),
      queryClient.invalidateQueries({ queryKey: ["canary", changeId] }),
      queryClient.invalidateQueries({ queryKey: ["changes"] }),
      queryClient.invalidateQueries({ queryKey: ["incidents"] }),
      queryClient.invalidateQueries({ queryKey: ["integrations"] }),
      queryClient.invalidateQueries({ queryKey: ["versions"] }),
    ]);
  };
  const done = (message: string) => async () => {
    await invalidate();
    setDialog(null);
    toast.success(message);
  };
  const approve = useMutation({ mutationFn: (d: Decision) => api.changes.approve(changeId, d), onSuccess: done("Change approved") });
  const reject = useMutation({ mutationFn: (d: Decision) => api.changes.reject(changeId, d), onSuccess: done("Change rejected") });
  const abort = useMutation({ mutationFn: (d: Decision) => api.changes.abort(changeId, d), onSuccess: done("Canary aborted") });
  const startCanary = useMutation({
    mutationFn: (fraction: number | undefined) => api.changes.startCanary(changeId, fraction),
    onSuccess: done("Canary started"),
  });
  const promote = useMutation({
    mutationFn: (d: Decision & { force: boolean }) => api.changes.promote(changeId, d),
    onSuccess: done("Promoted. The candidate is the published version now."),
  });

  if (change.error) return <ErrorState error={change.error} retry={() => void change.refetch()} />;
  if (!change.data) return <Loading />;

  const record = change.data;
  const display = currentVersion(versions.data)?.manifest.display_name ?? record.integration;
  const status = changeStatus[record.status];

  return (
    <>
      <PageHeader
        back={{ to: "/drift?tab=changes", label: "Change requests" }}
        title={
          <>
            {display} <span className="font-mono font-medium">{record.base_version}</span>{" "}
            <span className="font-normal text-ink-2">to</span>{" "}
            <span className="font-mono font-medium">{record.candidate_version}</span>
          </>
        }
        meta={
          <>
            <Status tone={status.tone}>{status.label}</Status>
            <Status tone={riskTone[record.risk_class]}>{record.risk_class} risk</Status>
            <span>{record.drift_class} drift</span>
            <span>Change {record.id}</span>
            {record.incident_id ? (
              <Link className="hover:underline" to={`/drift/incidents/${record.incident_id}`}>
                Incident {record.incident_id}
              </Link>
            ) : null}
            <span className="basis-full text-xs text-ink-3">Strategy {record.strategy}</span>
          </>
        }
        actions={
          <>
            {record.status === "pending" ? (
              <>
                <Button variant="danger" onClick={() => setDialog("reject")}>
                  Reject
                </Button>
                <Button variant="primary" onClick={() => setDialog("approve")}>
                  Approve
                </Button>
              </>
            ) : null}
            {record.status === "approved" ? (
              <>
                <Button onClick={() => setDialog("promote")}>Promote without canary</Button>
                <Button variant="primary" onClick={() => setDialog("canary")}>
                  Start canary
                </Button>
              </>
            ) : null}
            {record.status === "canary" ? (
              <>
                <Button variant="danger" onClick={() => setDialog("abort")}>
                  Abort canary
                </Button>
                <Button variant="primary" onClick={() => setDialog("promote")}>
                  Promote
                </Button>
              </>
            ) : null}
          </>
        }
      />

      <ChangeLifecycle status={record.status} />

      <div className="mt-4 space-y-3">
        {!record.verified ? (
          <Notice tone="bad" title="The candidate did not pass verification">
            Approving it would publish a manifest that fails against the API as it behaves now.
          </Notice>
        ) : null}
        {record.decided_by ? (
          <p className="text-sm text-ink-2">
            {record.status === "rejected" ? "Rejected" : "Approved"} by {record.decided_by}
            {record.decided_at ? ` on ${formatDateTime(record.decided_at)}` : ""}
            {record.auto_approved ? " through the approval policy" : ""}
            {record.decision_note ? `: ${record.decision_note}` : "."}
          </p>
        ) : null}
        {record.promoted_at ? (
          <p className="text-sm text-ink-2">
            Promoted on {formatDateTime(record.promoted_at)}. {record.candidate_version} became the published version.
          </p>
        ) : null}
      </div>

      <Tabs defaultValue="diff" className="mt-8">
        <TabsList>
          <TabsTrigger value="diff">Diff ({record.diff.length})</TabsTrigger>
          <TabsTrigger value="verification">Verification</TabsTrigger>
          <TabsTrigger value="canary">Canary</TabsTrigger>
        </TabsList>
        <TabsContent value="diff">
          <DiffView ops={record.diff} />
        </TabsContent>
        <TabsContent value="verification">
          {record.verification ? (
            <VerificationReportView report={record.verification} />
          ) : (
            <p className="text-sm text-ink-3">No verification report was stored with this change.</p>
          )}
        </TabsContent>
        <TabsContent value="canary">
          {!record.canary_started_at ? (
            <p className="text-sm text-ink-3">
              No canary yet. Once approved, a fraction of live calls can be routed to the candidate and compared
              with the published version before promotion.
            </p>
          ) : canary.error ? (
            <ErrorState error={canary.error} retry={() => void canary.refetch()} />
          ) : !canary.data ? (
            <Loading />
          ) : (
            <CanaryView report={canary.data} startedAt={record.canary_started_at} />
          )}
        </TabsContent>
      </Tabs>

      <DecisionDialog
        open={dialog === "approve"}
        onOpenChange={(open) => setDialog(open ? "approve" : null)}
        title={`Approve ${record.candidate_version}?`}
        description="Approval allows a canary or promotion. The published version does not change yet."
        confirmLabel="Approve"
        pending={approve.isPending}
        error={approve.error}
        onConfirm={(decision) => approve.mutate(decision)}
      />
      <DecisionDialog
        open={dialog === "reject"}
        onOpenChange={(open) => setDialog(open ? "reject" : null)}
        title={`Reject ${record.candidate_version}?`}
        description="The candidate version stays in the registry as rejected and the incident goes back to needing a person."
        confirmLabel="Reject"
        variant="danger"
        pending={reject.isPending}
        error={reject.error}
        onConfirm={(decision) => reject.mutate(decision)}
      />
      <DecisionDialog
        open={dialog === "abort"}
        onOpenChange={(open) => setDialog(open ? "abort" : null)}
        title="Abort the canary?"
        description="All traffic returns to the published version."
        confirmLabel="Abort canary"
        variant="danger"
        pending={abort.isPending}
        error={abort.error}
        onConfirm={(decision) => abort.mutate(decision)}
      />
      <CanaryDialog
        open={dialog === "canary"}
        onOpenChange={(open) => setDialog(open ? "canary" : null)}
        pending={startCanary.isPending}
        error={startCanary.error}
        onConfirm={(fraction) => startCanary.mutate(fraction)}
      />
      <PromoteDialog
        open={dialog === "promote"}
        onOpenChange={(open) => setDialog(open ? "promote" : null)}
        candidate={record.candidate_version}
        verdict={canary.data?.verdict}
        pending={promote.isPending}
        error={promote.error}
        onConfirm={(decision) => promote.mutate(decision)}
      />
    </>
  );
}

const steps = ["Proposed", "Approved", "Canary", "Promoted"] as const;

/** The gate a repair passes through. Terminal branches are shown where they leave the sequence. */
function ChangeLifecycle({ status }: { status: ChangeStatus }) {
  const reached: Record<ChangeStatus, number> = {
    pending: 0,
    rejected: 0,
    failed: 0,
    approved: 1,
    canary: 2,
    aborted: 2,
    promoted: 3,
    rolled_back: 3,
  };
  const at = reached[status];
  const branch =
    status === "rejected" ? "Rejected" : status === "failed" ? "Failed" : status === "aborted" ? "Aborted" : status === "rolled_back" ? "Rolled back" : null;
  return (
    <ol className="flex flex-wrap items-center gap-3 text-sm">
      {steps.map((step, index) => {
        const done = index < at;
        const current = index === at;
        return (
          <li key={step} className="flex items-center gap-3">
            <span className={cn("flex items-center gap-2", current ? "font-medium text-ink" : done ? "text-ink-2" : "text-ink-3")}>
              <span
                aria-hidden
                className={cn(
                  "inline-block size-2 rounded-full",
                  current && branch ? "bg-bad" : done || current ? "bg-ink" : "border border-line-strong",
                )}
              />
              {current && branch ? branch : step}
            </span>
            {index < steps.length - 1 ? <span aria-hidden className={cn("h-px w-8", done ? "bg-ink" : "bg-line-strong")} /> : null}
          </li>
        );
      })}
    </ol>
  );
}

function DiffView({ ops }: { ops: DiffOp[] }) {
  if (!ops.length) return <p className="text-sm text-ink-3">The candidate is identical to the base version.</p>;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Change</TH>
          <TH>Path</TH>
          <TH>From</TH>
          <TH>To</TH>
        </TR>
      </THead>
      <TBody>
        {ops.map((op, index) => (
          <TR key={index} className="align-top">
            <TD className="whitespace-nowrap">
              <Status tone={op.op === "remove" ? "bad" : op.op === "add" ? "ok" : "warn"}>{op.op}</Status>
            </TD>
            <TD>
              <Mono className="break-all">{op.path}</Mono>
            </TD>
            <TD className="max-w-xs">
              <DiffValue value={op.from} />
            </TD>
            <TD className="max-w-xs">
              <DiffValue value={op.to} />
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function DiffValue({ value }: { value: unknown }) {
  if (value === undefined) return <span className="text-ink-3">none</span>;
  if (value === null || typeof value !== "object") return <Mono className="break-all">{JSON.stringify(value)}</Mono>;
  const text = JSON.stringify(value, null, 2);
  if (text.length < 120) return <Mono className="break-all">{JSON.stringify(value)}</Mono>;
  return (
    <details>
      <summary className="text-xs text-ink-2 hover:text-ink">{Array.isArray(value) ? `${value.length} items` : `${Object.keys(value).length} keys`}</summary>
      <CodeBlock className="mt-2" value={value} maxHeight="16rem" />
    </details>
  );
}

function CanaryView({ report, startedAt }: { report: CanaryReport; startedAt: string }) {
  const verdictTone = report.verdict === "pass" ? "ok" : report.verdict === "fail" ? "bad" : "warn";
  const rows = [
    { arm: "Published", stats: report.base },
    { arm: "Candidate", stats: report.candidate },
  ];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
        <Status tone={verdictTone} className="font-medium">
          {report.verdict === "pass" ? "Passing" : report.verdict === "fail" ? "Failing" : "Not enough calls yet"}
        </Status>
        <span className="text-ink-2">{Math.round(report.fraction * 100)}% of calls go to the candidate</span>
        <span className="text-ink-2">needs {report.min_calls} calls per arm</span>
        <span className="text-ink-3">since {formatDateTime(startedAt)}</span>
      </div>
      <p className="text-sm text-ink-2">{report.reason}</p>
      <Table>
        <THead>
          <TR>
            <TH>Arm</TH>
            <TH>Version</TH>
            <TH className="text-right">Calls</TH>
            <TH className="text-right">Failures</TH>
            <TH className="text-right">Validation errors</TH>
            <TH className="text-right">Drift events</TH>
          </TR>
        </THead>
        <TBody>
          {rows.map((row) => (
            <TR key={row.arm}>
              <TD>{row.arm}</TD>
              <TD>
                <Mono>{row.stats.version}</Mono>
              </TD>
              <TD numeric>{row.stats.calls}</TD>
              <TD numeric>{row.stats.failures}</TD>
              <TD numeric>{row.stats.validation_errors}</TD>
              <TD numeric>{row.stats.drift_events}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function CanaryDialog({
  open,
  onOpenChange,
  pending,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  error: unknown;
  onConfirm: (fraction: number | undefined) => void;
}) {
  const [fraction, setFraction] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    const value = Number(fraction);
    onConfirm(fraction && value > 0 && value <= 1 ? value : undefined);
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Start a canary</DialogTitle>
            <DialogDescription>
              A fraction of live calls is routed to the candidate. Error and validation rates are compared with
              the published version before anything is promoted.
            </DialogDescription>
          </DialogHeader>
          <Field label="Fraction of calls" htmlFor="canary-fraction" hint="Between 0 and 1. Leave empty for the server default.">
            <Input id="canary-fraction" type="number" step="0.05" min={0.01} max={1} value={fraction} onChange={(event) => setFraction(event.target.value)} placeholder="0.1" />
          </Field>
          {error ? <Notice tone="bad">{errorMessage(error)}</Notice> : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={pending}>
              Start canary
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PromoteDialog({
  open,
  onOpenChange,
  candidate,
  verdict,
  pending,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidate: string;
  verdict: CanaryReport["verdict"] | undefined;
  pending: boolean;
  error: unknown;
  onConfirm: (decision: Decision & { force: boolean }) => void;
}) {
  const [force, setForce] = useState(false);
  return (
    <DecisionDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Promote ${candidate}?`}
      description="The candidate becomes the published version and the current one is superseded. Rollback restores it in one step."
      confirmLabel="Promote"
      pending={pending}
      error={error}
      onConfirm={(decision) => onConfirm({ ...decision, force })}
      extra={
        verdict !== "pass" ? (
          <Checkbox
            label={verdict ? `Promote although the canary is ${verdict === "fail" ? "failing" : "inconclusive"}` : "Promote without a canary"}
            checked={force}
            onChange={(event) => setForce(event.target.checked)}
          />
        ) : null
      }
    />
  );
}
