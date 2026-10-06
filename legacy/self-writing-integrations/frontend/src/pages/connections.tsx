import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { KeyValueFields } from "@/components/key-value-fields";
import { NewTenantDialog } from "@/components/shell/shell";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Field, Input, Select } from "@/components/ui/field";
import { ErrorState, Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Status, Tag } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { authLabel, connectionStatus, formatDateTime, formatRelative, pluralize } from "@/lib/format";
import { useConnections, useIntegrationManifests } from "@/lib/hooks";
import { useTenant } from "@/lib/tenant";
import type { IntegrationSummary, VersionRecord } from "@/lib/types";

export function ConnectionsPage() {
  const { tenant, tenantId, isLoading: tenantsLoading } = useTenant();
  const connections = useConnections(tenantId);
  const manifests = useIntegrationManifests();
  const [newOpen, setNewOpen] = useState(false);
  const [appOpen, setAppOpen] = useState(false);
  const [tenantOpen, setTenantOpen] = useState(false);

  const displayName = (integrationName: string) =>
    manifests.current[integrationName]?.manifest.display_name ?? integrationName;

  return (
    <>
      <PageHeader
        title="Connections"
        meta={
          tenant ? (
            <span>
              {connections.data ? pluralize(connections.data.length, "connection") : "Connections"} for {tenant.name}
            </span>
          ) : null
        }
        actions={
          tenant ? (
            <>
              <Button onClick={() => setAppOpen(true)}>Register OAuth app</Button>
              <Button variant="primary" onClick={() => setNewOpen(true)}>
                New connection
              </Button>
            </>
          ) : null
        }
      />
      {tenantsLoading ? (
        <Loading />
      ) : !tenant ? (
        <EmptyState
          title="Create a tenant first"
          actions={
            <Button variant="primary" onClick={() => setTenantOpen(true)}>
              New tenant
            </Button>
          }
        >
          <p>
            Connections, credentials and audit history belong to a tenant. Each tenant gets its own encryption key,
            so one tenant's tokens can never be read as another's.
          </p>
        </EmptyState>
      ) : connections.error ? (
        <ErrorState error={connections.error} retry={() => void connections.refetch()} />
      ) : !connections.data ? (
        <Loading />
      ) : connections.data.length === 0 ? (
        <EmptyState
          title={`No connections for ${tenant.name}`}
          actions={
            <>
              <Button variant="primary" onClick={() => setNewOpen(true)}>
                New connection
              </Button>
              <Button onClick={() => setAppOpen(true)}>Register OAuth app</Button>
            </>
          }
        >
          <p>
            A connection binds this tenant to one integration. For OAuth integrations, register the platform's
            OAuth app once, create the connection, then send the tenant to the consent screen. Tokens refresh on
            their own from then on.
          </p>
        </EmptyState>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Integration</TH>
              <TH>Status</TH>
              <TH>Granted scopes</TH>
              <TH>Token expires</TH>
              <TH>Last refreshed</TH>
              <TH className="text-right">Refreshes</TH>
              <TH>Created</TH>
            </TR>
          </THead>
          <TBody>
            {connections.data.map((connection) => {
              const status = connectionStatus[connection.status];
              return (
                <TR key={connection.id}>
                  <TD>
                    <Link to={`/connections/${connection.id}`} className="font-medium hover:underline">
                      {displayName(connection.integration_name)}
                    </Link>
                    <div className="font-mono text-xs text-ink-3">{connection.id}</div>
                  </TD>
                  <TD>
                    <Status tone={status.tone}>{status.label}</Status>
                  </TD>
                  <TD>
                    {connection.granted_scopes.length ? (
                      <span className="flex flex-wrap gap-1">
                        {connection.granted_scopes.map((scope) => (
                          <Tag key={scope}>{scope}</Tag>
                        ))}
                      </span>
                    ) : (
                      <span className="text-ink-3">None</span>
                    )}
                  </TD>
                  <TD className="whitespace-nowrap text-ink-2" title={formatDateTime(connection.token_expires_at)}>
                    {connection.token_expires_at ? formatRelative(connection.token_expires_at) : <span className="text-ink-3">No token</span>}
                  </TD>
                  <TD className="whitespace-nowrap text-ink-2">
                    {connection.last_refreshed_at ? formatRelative(connection.last_refreshed_at) : <span className="text-ink-3">Never</span>}
                  </TD>
                  <TD numeric>{connection.refresh_count}</TD>
                  <TD className="whitespace-nowrap text-ink-2">{formatDateTime(connection.created_at)}</TD>
                </TR>
              );
            })}
          </TBody>
        </Table>
      )}

      {tenantId ? (
        <>
          <NewConnectionDialog
            open={newOpen}
            onOpenChange={setNewOpen}
            tenantId={tenantId}
            integrations={manifests.integrations}
            current={manifests.current}
          />
          <RegisterAppDialog
            open={appOpen}
            onOpenChange={setAppOpen}
            integrations={manifests.integrations}
            current={manifests.current}
          />
        </>
      ) : null}
      <NewTenantDialog open={tenantOpen} onOpenChange={setTenantOpen} />
    </>
  );
}

function NewConnectionDialog({
  open,
  onOpenChange,
  tenantId,
  integrations,
  current,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tenantId: string;
  integrations: IntegrationSummary[];
  current: Record<string, VersionRecord | undefined>;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [integration, setIntegration] = useState("");
  const [config, setConfig] = useState<Record<string, string>>({});
  const chosen = integration || integrations[0]?.name || "";
  const manifest = current[chosen]?.manifest;

  const create = useMutation({
    mutationFn: () => api.connections.create({ tenant_id: tenantId, integration_name: chosen, config }),
    onSuccess: async (connection) => {
      await queryClient.invalidateQueries({ queryKey: ["connections"] });
      await queryClient.invalidateQueries({ queryKey: ["tenant-audit"] });
      onOpenChange(false);
      if (connection.status === "pending_consent") {
        toast.success("Connection created. It needs the tenant's consent before it can be used.");
        navigate(`/connections/${connection.id}/consent`);
      } else {
        toast.success("Connection created and active");
        navigate(`/connections/${connection.id}`);
      }
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (chosen) create.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>New connection</DialogTitle>
            <DialogDescription>Bind this tenant to an integration.</DialogDescription>
          </DialogHeader>
          {integrations.length === 0 ? (
            <Notice>
              No integrations in the registry yet.{" "}
              <Link className="underline" to="/integrations/new">
                Add one
              </Link>{" "}
              before creating a connection.
            </Notice>
          ) : (
            <Field
              label="Integration"
              htmlFor="conn-integration"
              hint={
                manifest?.auth.type === "oauth2"
                  ? "Uses OAuth. After creating the connection you go to the consent screen."
                  : manifest
                    ? `Uses ${authLabel(manifest.auth).toLowerCase()}. The connection is active at once; secrets are supplied per call.`
                    : undefined
              }
            >
              <Select
                id="conn-integration"
                value={chosen}
                onChange={(event) => {
                  setIntegration(event.target.value);
                  setConfig({});
                }}
              >
                {integrations.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.display_name} ({authLabel(current[item.name]?.manifest.auth).toLowerCase()})
                  </option>
                ))}
              </Select>
            </Field>
          )}
          {manifest?.config_vars.length ? (
            <KeyValueFields
              idPrefix="conn-config"
              specs={manifest.config_vars.map((name) => ({
                name,
                required: true,
                description: `Fills {${name}} in the base URL`,
              }))}
              values={config}
              onChange={setConfig}
            />
          ) : null}
          {create.error ? (
            <Notice tone="bad" title="Could not create the connection">
              {errorMessage(create.error)}
            </Notice>
          ) : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!chosen || create.isPending}>
              {create.isPending ? "Creating" : "Create connection"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RegisterAppDialog({
  open,
  onOpenChange,
  integrations,
  current,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  integrations: IntegrationSummary[];
  current: Record<string, VersionRecord | undefined>;
}) {
  const oauthIntegrations = integrations.filter((item) => current[item.name]?.manifest.auth.type === "oauth2");
  const [integration, setIntegration] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("");
  const chosen = integration || oauthIntegrations[0]?.name || "";

  const register = useMutation({
    mutationFn: () =>
      api.apps.register({
        integration_name: chosen,
        client_id: clientId.trim(),
        client_secret: clientSecret,
        redirect_uri: redirectUri.trim() || undefined,
      }),
    onSuccess: (app) => {
      toast.success(`Registered the ${app.integration_name} app`, {
        description: `Redirect URI ${app.redirect_uri}. Make sure the provider has the same one.`,
      });
      setClientId("");
      setClientSecret("");
      setRedirectUri("");
      onOpenChange(false);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (chosen && clientId.trim() && clientSecret) register.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Register OAuth app</DialogTitle>
            <DialogDescription>
              The platform holds one OAuth client per integration, shared by every tenant. The client secret is
              encrypted with the vault master key and never shown again.
            </DialogDescription>
          </DialogHeader>
          {oauthIntegrations.length === 0 ? (
            <Notice>No OAuth integrations in the registry yet. Import one, such as the Gusto reference manifest.</Notice>
          ) : (
            <>
              <Field label="Integration" htmlFor="app-integration">
                <Select id="app-integration" value={chosen} onChange={(event) => setIntegration(event.target.value)}>
                  {oauthIntegrations.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.display_name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Client id" htmlFor="app-client-id">
                <Input
                  id="app-client-id"
                  className="font-mono text-[13px]"
                  autoComplete="off"
                  value={clientId}
                  onChange={(event) => setClientId(event.target.value)}
                />
              </Field>
              <Field label="Client secret" htmlFor="app-client-secret">
                <Input
                  id="app-client-secret"
                  type="password"
                  className="font-mono text-[13px]"
                  autoComplete="off"
                  value={clientSecret}
                  onChange={(event) => setClientSecret(event.target.value)}
                />
              </Field>
              <Field
                label="Redirect URI"
                htmlFor="app-redirect"
                hint="Leave empty to use PUBLIC_BASE_URL/oauth/callback from the API settings"
              >
                <Input
                  id="app-redirect"
                  className="font-mono text-[13px]"
                  autoComplete="off"
                  value={redirectUri}
                  onChange={(event) => setRedirectUri(event.target.value)}
                  placeholder="https://platform.example/oauth/callback"
                />
              </Field>
            </>
          )}
          {register.error ? (
            <Notice tone="bad" title="Could not register the app">
              {errorMessage(register.error)}
            </Notice>
          ) : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button
              type="submit"
              variant="primary"
              disabled={!chosen || !clientId.trim() || !clientSecret || register.isPending}
            >
              {register.isPending ? "Registering" : "Register app"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
