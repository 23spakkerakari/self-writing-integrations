import { Fragment } from "react";
import { Mono, Status } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { formatDateTime, formatDuration, pluralize } from "@/lib/format";
import type { VerificationReport } from "@/lib/types";

/** The verification ledger: one row per endpoint, with errors and warnings listed under the row. */
export function VerificationReportView({ report }: { report: VerificationReport }) {
  const failed = report.checks.filter((check) => !check.passed).length;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
        <Status tone={report.passed ? "ok" : "bad"} className="font-medium">
          {report.passed ? "Passed" : "Failed"} on {report.mode}
        </Status>
        <span className="text-ink-2">
          {pluralize(report.checks.length, "endpoint")} checked{failed ? `, ${failed} failed` : ""}
        </span>
        <span className="text-ink-3">
          {formatDateTime(report.started_at)}, took {formatDuration(report.started_at, report.finished_at)}
        </span>
      </div>
      <Table>
        <THead>
          <TR>
            <TH>Endpoint</TH>
            <TH>Result</TH>
            <TH className="text-right">HTTP</TH>
            <TH className="text-right">Pages</TH>
            <TH className="text-right">Records</TH>
            <TH className="text-right">Canonical</TH>
          </TR>
        </THead>
        <TBody>
          {report.checks.map((check) => (
            <Fragment key={check.endpoint_id}>
              <TR className={check.errors.length || check.warnings.length ? "border-b-0" : undefined}>
                <TD>
                  <Mono>{check.endpoint_id}</Mono>
                </TD>
                <TD>
                  <Status tone={check.passed ? "ok" : "bad"}>{check.passed ? "Passed" : "Failed"}</Status>
                </TD>
                <TD numeric>{check.status_code ?? "none"}</TD>
                <TD numeric>{check.pages}</TD>
                <TD numeric>{check.records}</TD>
                <TD numeric>{check.canonical}</TD>
              </TR>
              {check.errors.length || check.warnings.length ? (
                <TR>
                  <TD colSpan={6} className="bg-surface-2 pt-0 pb-3">
                    <ul className="space-y-1 text-xs">
                      {check.errors.map((error, index) => (
                        <li key={`e${index}`} className="flex gap-2">
                          <span className="shrink-0 font-medium text-bad">Error</span>
                          <span className="font-mono text-ink-2 break-all">{error}</span>
                        </li>
                      ))}
                      {check.warnings.map((warning, index) => (
                        <li key={`w${index}`} className="flex gap-2">
                          <span className="shrink-0 font-medium text-warn">Warning</span>
                          <span className="font-mono text-ink-2 break-all">{warning}</span>
                        </li>
                      ))}
                    </ul>
                  </TD>
                </TR>
              ) : null}
            </Fragment>
          ))}
        </TBody>
      </Table>
    </div>
  );
}
