import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { AuditTimeline } from "@/components/audit-timeline";
import { CallConsole } from "@/components/call-console";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Facts } from "@/components/ui/facts";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Mono, Status, Tag } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage } from "@/lib/api";
import { connectionStatus, formatDateTime, formatRelative } from "@/lib/format";
import { currentVersion, useConnection, useHealth, useVersions } from "@/lib/hooks";
import { useTenant } from "@/lib/tenant";

export function ConnectionPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const health = useHealth();
  const { tenants } = useTenant();
  const connection = useConnection(id);
  const versions = useVersions(connection.data?.integration_name);
  const audit = useQuery({ queryKey: ["audit", id], queryFn: () => api.connections.audit(id), enabled: !!id });
  const [revokeOpen, setRevokeOpen] = useState(false);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["connection", id] }),
      queryClient.invalidateQueries({ queryKey: ["connections"] }),
      queryClient.invalidateQueries({ queryKey: ["audit", id] }),
      queryClient.invalidateQueries({ queryKey: ["tenant-audit"] }),
      queryClient.invalidateQueries({ queryKey: ["notifications"] }),
    ]);
  };
  const refresh = useMutation({
    mutationFn: () => api.connections.refresh(id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Token refreshed");
    },
    onError: async (error) => {
      await invalidate();
      toast.error(errorMessage(error));
    },
  });
  const revoke = useMutation({
    mutationFn: () => api.connections.revoke(id),
    onSuccess: async () => {
      await invalidate();
      setRevokeOpen(false);
      toast.success("Connection revoked and its credentials destroyed");
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const revokeAtProvider = useMutation({
    mutationFn: () => api.mock.revokeAtProvider(connection.data!.integration_name),
    onSuccess: () =>
      toast.message("The mock provider revoked the app", {
        description: "The next refresh fails with invalid_grant and flips this connection to needs re-consent.",
      }),
    onError: (error) => toast.error(errorMessage(error)),
  });

  if (connection.error) return <ErrorState error={connection.error} retry={() => void connection.refetch()} />;
  if (!connection.data) return <Loading />;

  const record = connection.data;
  const manifest = currentVersion(versions.data)?.manifest;
  const display = manifest?.display_name ?? record.integration_name;
  const tenant = tenants.find((t) => t.id === record.tenant_id);
  const status = connectionStatus[record.status];
  const isOAuth = manifest?.auth.type === "oauth2";
  const consentPath = `/connections/${id}/consent`;

  return (
    <>
      <PageHeader
        back={{ to: "/connections", label: "Connections" }}
        title={
          <>
            {display}
            {tenant ? <span className="font-normal text-ink-2"> for {tenant.name}</span> : null}
          </>
        }
        block={[
          { label: "Status", value: <Status tone={status.tone}>{status.label}</Status> },
          { label: "Connection id", value: <Mono>{record.id}</Mono> },
        ]}
        actions={
          <>
            {record.status === "pending_consent" && isOAuth ? (
              <Button variant="primary" onClick={() => navigate(consentPath)}>
                Start consent
              </Button>
            ) : null}
            {(record.status === "needs_reconsent" || record.status === "revoked") && isOAuth ? (
              <Button variant="primary" onClick={() => navigate(consentPath)}>
                Reconnect
              </Button>
            ) : null}
            {record.status === "active" && isOAuth ? (
              <Button disabled={refresh.isPending} onClick={() => refresh.mutate()}>
                {refresh.isPending ? "Refreshing" : "Refresh token"}
              </Button>
            ) : null}
            {record.status === "active" ? (
              <Button variant="danger" onClick={() => setRevokeOpen(true)}>
                Revoke
              </Button>
            ) : null}
            {health.data?.mode === "mock" && isOAuth && record.status === "active" ? (
              <Button variant="ghost" disabled={revokeAtProvider.isPending} onClick={() => revokeAtProvider.mutate()}>
                Simulate revocation at the provider
              </Button>
            ) : null}
          </>
        }
      />

      {record.status === "needs_reconsent" ? (
        <Notice tone="bad" title="The provider rejected the refresh token" className="mb-6">
          Credentials for this connection were destroyed and refresh attempts stopped. The tenant has to approve
          access again before calls can resume.
        </Notice>
      ) : null}
      {record.status === "pending_consent" && isOAuth ? (
        <Notice tone="warn" className="mb-6">
          Waiting for the tenant to approve access at {display}. Nothing is stored in the vault until they do.
        </Notice>
      ) : null}

      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <Tabs defaultValue="audit">
          <TabsList>
            <TabsTrigger value="audit">Audit log</TabsTrigger>
            <TabsTrigger value="call">Call</TabsTrigger>
          </TabsList>
          <TabsContent value="audit">
            {audit.error ? (
              <ErrorState error={audit.error} retry={() => void audit.refetch()} />
            ) : !audit.data ? (
              <Loading />
            ) : (
              <AuditTimeline events={audit.data} />
            )}
          </TabsContent>
          <TabsContent value="call">
            {!manifest ? (
              <Loading />
            ) : (
              <CallConsole
                manifest={manifest}
                mode="connection"
                disabledReason={
                  !isOAuth ? (
                    <>
                      Calls through a connection use tokens from the vault, which only OAuth integrations have.
                      Call {display} from{" "}
                      <Link className="underline" to={`/integrations/${record.integration_name}`}>
                        its integration page
                      </Link>{" "}
                      instead.
                    </>
                  ) : record.status !== "active" ? (
                    `This connection is ${status.label.toLowerCase()}. Calls need an active connection.`
                  ) : undefined
                }
                onCall={(input) =>
                  api.connections.call(id, {
                    endpoint_id: input.endpoint_id,
                    params: input.params,
                    paginate: input.paginate,
                  })
                }
              />
            )}
          </TabsContent>
        </Tabs>

        <aside>
          <h2 className="mb-3 text-base font-medium">Details</h2>
          <Facts
            items={[
              {
                label: "Integration",
                value: (
                  <Link className="hover:underline" to={`/integrations/${record.integration_name}`}>
                    {display}
                  </Link>
                ),
              },
              { label: "Tenant", value: tenant?.name ?? <Mono>{record.tenant_id}</Mono> },
              {
                label: "Granted scopes",
                value: record.granted_scopes.length ? (
                  <span className="flex flex-wrap gap-1">
                    {record.granted_scopes.map((scope) => (
                      <Tag key={scope}>{scope}</Tag>
                    ))}
                  </span>
                ) : null,
              },
              {
                label: "Token expires",
                value: record.token_expires_at ? (
                  <span title={formatDateTime(record.token_expires_at)}>{formatRelative(record.token_expires_at)}</span>
                ) : null,
              },
              {
                label: "Last refreshed",
                value: record.last_refreshed_at ? formatDateTime(record.last_refreshed_at) : null,
              },
              { label: "Refreshes", value: String(record.refresh_count) },
              {
                label: "Config",
                value: Object.keys(record.config).length ? (
                  <span className="flex flex-wrap gap-1">
                    {Object.entries(record.config).map(([key, value]) => (
                      <Tag key={key}>
                        {key}={String(value)}
                      </Tag>
                    ))}
                  </span>
                ) : null,
              },
              { label: "Created", value: formatDateTime(record.created_at) },
              { label: "Updated", value: formatDateTime(record.updated_at) },
            ]}
          />
        </aside>
      </div>

      <Dialog open={revokeOpen} onOpenChange={setRevokeOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke this connection?</DialogTitle>
            <DialogDescription>
              Its credentials are destroyed and calls stop until the tenant consents again. The audit log keeps the
              full history.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setRevokeOpen(false)}>Cancel</Button>
            <Button variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate()}>
              {revoke.isPending ? "Revoking" : "Revoke connection"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
