import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState, Loading } from "@/components/ui/loading";
import { PageHeader } from "@/components/ui/page-header";
import { Mono, Status } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { authLabel, formatRelative, pluralize } from "@/lib/format";
import { useIntegrationManifests } from "@/lib/hooks";
import type { VersionRecord } from "@/lib/types";

export function IntegrationsPage() {
  const { integrations, current, isLoading, error, refetch } = useIntegrationManifests();

  return (
    <>
      <PageHeader
        title="Integrations"
        meta={integrations.length ? <span>{pluralize(integrations.length, "integration")} in the registry</span> : null}
        actions={
          <Button asChild variant="primary">
            <Link to="/integrations/new">Add integration</Link>
          </Button>
        }
      />
      {error ? (
        <ErrorState error={error} retry={() => void refetch()} />
      ) : isLoading ? (
        <Loading />
      ) : integrations.length === 0 ? (
        <EmptyState
          title="No integrations yet"
          actions={
            <>
              <Button asChild variant="primary">
                <Link to="/integrations/new?tab=import">Import a manifest</Link>
              </Button>
              <Button asChild>
                <Link to="/integrations/new?tab=synthesize">Synthesize from a spec</Link>
              </Button>
            </>
          }
        >
          <p>
            Import a manifest you already have, or hand the agent an API spec and let it write and verify one. The
            reference manifests in the repository work as a first import:
          </p>
          <CodeBlock
            value={
              "cd backend\npython -m app.cli import manifests/bamboohr.yaml\npython -m app.cli import manifests/gusto.yaml"
            }
          />
        </EmptyState>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Integration</TH>
              <TH>Auth</TH>
              <TH>Published</TH>
              <TH>Latest</TH>
              <TH>Endpoints</TH>
              <TH>Last verification</TH>
            </TR>
          </THead>
          <TBody>
            {integrations.map((integration) => {
              const record = current[integration.name];
              return (
                <TR key={integration.name}>
                  <TD>
                    <Link to={`/integrations/${integration.name}`} className="font-medium hover:underline">
                      {integration.display_name}
                    </Link>
                    <div className="font-mono text-xs text-ink-3">{integration.name}</div>
                  </TD>
                  <TD className="text-ink-2">{record ? authLabel(record.manifest.auth) : ""}</TD>
                  <TD>
                    {integration.published_version ? (
                      <Mono>{integration.published_version}</Mono>
                    ) : (
                      <span className="text-ink-3">Not published</span>
                    )}
                  </TD>
                  <TD>
                    <Mono>{integration.latest_version}</Mono>
                    <span className="ml-1.5 text-xs text-ink-3">of {integration.version_count}</span>
                  </TD>
                  <TD className="text-ink-2">{record ? record.manifest.endpoints.length : ""}</TD>
                  <TD>
                    <LastVerification record={record} />
                  </TD>
                </TR>
              );
            })}
          </TBody>
        </Table>
      )}
    </>
  );
}

function LastVerification({ record }: { record: VersionRecord | undefined }) {
  if (!record) return null;
  const report = record.verification;
  if (!report) return <Status tone="idle">Not verified</Status>;
  return (
    <div>
      <Status tone={report.passed ? "ok" : "bad"}>
        {report.passed ? "Passed" : "Failed"} on {report.mode}
      </Status>
      <div className="text-xs text-ink-3">{formatRelative(report.finished_at)}</div>
    </div>
  );
}
