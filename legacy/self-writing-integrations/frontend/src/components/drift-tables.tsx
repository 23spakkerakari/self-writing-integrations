import { Link } from "react-router-dom";
import { Mono, Status } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { changeStatus, formatDateTime, formatRelative, incidentStatus, kindLabel, riskTone } from "@/lib/format";
import type { ChangeRequest, DriftIncident } from "@/lib/types";

export function IncidentsTable({
  incidents,
  displayName = (name) => name,
}: {
  incidents: DriftIncident[];
  displayName?: (integration: string) => string;
}) {
  const ordered = [...incidents].sort((a, b) => b.id - a.id);
  return (
    <Table>
      <THead>
        <TR>
          <TH>Incident</TH>
          <TH>Integration</TH>
          <TH>Endpoint</TH>
          <TH>Class</TH>
          <TH>Risk</TH>
          <TH>Status</TH>
          <TH className="text-right">Seen</TH>
          <TH>Last seen</TH>
          <TH>Change request</TH>
        </TR>
      </THead>
      <TBody>
        {ordered.map((incident) => {
          const status = incidentStatus[incident.status];
          return (
            <TR key={incident.id}>
              <TD>
                <Link to={`/drift/incidents/${incident.id}`} className="font-medium hover:underline">
                  {kindLabel(incident.kind)}
                </Link>
                <div className="text-xs text-ink-3">Incident {incident.id}</div>
              </TD>
              <TD>
                <Link to={`/integrations/${incident.integration}`} className="hover:underline">
                  {displayName(incident.integration)}
                </Link>
              </TD>
              <TD>
                <Mono>{incident.endpoint_id}</Mono>
              </TD>
              <TD className="text-ink-2">
                {incident.drift_class ?? <span className="text-ink-3">Not triaged</span>}
              </TD>
              <TD>
                {incident.risk_class ? (
                  <Status tone={riskTone[incident.risk_class]}>{incident.risk_class}</Status>
                ) : (
                  <span className="text-ink-3">Unknown</span>
                )}
              </TD>
              <TD>
                <Status tone={status.tone}>{status.label}</Status>
              </TD>
              <TD numeric>{incident.count}</TD>
              <TD className="whitespace-nowrap text-ink-2" title={formatDateTime(incident.last_seen)}>
                {formatRelative(incident.last_seen)}
              </TD>
              <TD>
                {incident.change_request_id ? (
                  <Link className="hover:underline" to={`/changes/${incident.change_request_id}`}>
                    Change {incident.change_request_id}
                  </Link>
                ) : (
                  <span className="text-ink-3">None</span>
                )}
              </TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}

export function ChangesTable({
  changes,
  displayName = (name) => name,
}: {
  changes: ChangeRequest[];
  displayName?: (integration: string) => string;
}) {
  const ordered = [...changes].sort((a, b) => b.id - a.id);
  return (
    <Table>
      <THead>
        <TR>
          <TH>Change</TH>
          <TH>Integration</TH>
          <TH>Versions</TH>
          <TH>Class</TH>
          <TH>Risk</TH>
          <TH>Verified</TH>
          <TH>Status</TH>
          <TH>Created</TH>
          <TH>Decided by</TH>
        </TR>
      </THead>
      <TBody>
        {ordered.map((change) => {
          const status = changeStatus[change.status];
          return (
            <TR key={change.id}>
              <TD>
                <Link to={`/changes/${change.id}`} className="font-medium hover:underline">
                  Change {change.id}
                </Link>
                <div className="max-w-64 truncate text-xs text-ink-3" title={change.strategy}>
                  {change.strategy}
                </div>
              </TD>
              <TD>
                <Link to={`/integrations/${change.integration}`} className="hover:underline">
                  {displayName(change.integration)}
                </Link>
              </TD>
              <TD className="whitespace-nowrap">
                <Mono>{change.base_version}</Mono> <span className="text-ink-3">to</span>{" "}
                <Mono>{change.candidate_version}</Mono>
              </TD>
              <TD className="text-ink-2">{change.drift_class}</TD>
              <TD>
                <Status tone={riskTone[change.risk_class]}>{change.risk_class}</Status>
              </TD>
              <TD>
                <Status tone={change.verified ? "ok" : "bad"}>{change.verified ? "Yes" : "No"}</Status>
              </TD>
              <TD>
                <Status tone={status.tone}>{status.label}</Status>
                {change.auto_approved ? <div className="text-xs text-ink-3">Auto-approved by policy</div> : null}
              </TD>
              <TD className="whitespace-nowrap text-ink-2">{formatDateTime(change.created_at)}</TD>
              <TD className="text-ink-2">{change.decided_by ?? <span className="text-ink-3">Nobody yet</span>}</TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}
