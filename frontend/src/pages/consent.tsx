import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Loading } from "@/components/ui/loading";
import { Notice } from "@/components/ui/notice";
import { Mono } from "@/components/ui/status";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, formatRelative, shortId } from "@/lib/format";
import { currentVersion, useConnection, useHealth, useVersions } from "@/lib/hooks";

/**
 * The human-in-the-loop step. Shows exactly which scopes will be requested and why, then hands
 * the person to the provider. Rendered without the console chrome because the tenant, not the
 * developer, is the reader.
 */
export function ConsentPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const health = useHealth();
  const connection = useConnection(id);
  const versions = useVersions(connection.data?.integration_name);
  const tenants = useQuery({ queryKey: ["tenants"], queryFn: api.tenants.list });
  const consent = useQuery({
    queryKey: ["consent", id],
    queryFn: () => api.connections.consent(id),
    enabled: !!connection.data,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });

  const manifest = currentVersion(versions.data)?.manifest;
  const display = manifest?.display_name ?? connection.data?.integration_name ?? "";
  const tenant = tenants.data?.find((t) => t.id === connection.data?.tenant_id);
  const tenantName = tenant?.name ?? "your organization";

  const approve = useMutation({
    mutationFn: async () => {
      const approval = await api.mock.authorize(consent.data!.authorize_url);
      return api.oauth.callback(approval.state, approval.code);
    },
    onSuccess: async (record) => {
      await queryClient.invalidateQueries({ queryKey: ["connection", id] });
      await queryClient.invalidateQueries({ queryKey: ["connections"] });
      await queryClient.invalidateQueries({ queryKey: ["audit", id] });
      toast.success(`${display} is connected for ${tenantName}`);
      navigate(`/connections/${record.id}`);
    },
  });

  let body: ReactNode;
  if (connection.error) {
    body = <Notice tone="bad">{errorMessage(connection.error)}</Notice>;
  } else if (consent.error) {
    body = (
      <Notice tone="bad" title="This connection cannot start consent">
        {errorMessage(consent.error)}
      </Notice>
    );
  } else if (!connection.data || !consent.data) {
    body = <Loading label="Preparing the request" />;
  } else {
    const request = consent.data;
    const baseScopes = manifest?.auth.type === "oauth2" ? manifest.auth.scopes : [];
    const rows = request.scopes.map((scope) => {
      const endpoints = manifest?.endpoints.filter((e) => e.scopes.includes(scope)).map((e) => e.id) ?? [];
      const why = baseScopes.includes(scope)
        ? "Required by every endpoint"
        : endpoints.length
          ? `Needed by ${endpoints.join(", ")}`
          : "Requested by the integration";
      return { scope, why };
    });

    body = (
      <>
        <h2 className="mt-8 text-sm font-medium">Permissions requested</h2>
        <ul className="mt-2 divide-y divide-line rounded-md border border-line">
          {rows.map((row) => (
            <li key={row.scope} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-4 py-2.5">
              <Mono className="font-medium">{row.scope}</Mono>
              <span className="text-xs text-ink-2">{row.why}</span>
            </li>
          ))}
          {rows.length === 0 ? <li className="px-4 py-2.5 text-sm text-ink-3">No scopes are requested.</li> : null}
        </ul>

        <h2 className="mt-8 text-sm font-medium">What happens next</h2>
        <ol className="mt-2 list-decimal space-y-1.5 pl-5 text-sm leading-6 text-ink-2">
          <li>You approve the request at {display}.</li>
          <li>The platform stores the tokens it receives encrypted, bound to {tenantName} only.</li>
          <li>Tokens refresh on their own. You are asked again only if {display} withdraws access.</li>
        </ol>

        <p className="mt-6 text-xs text-ink-3">
          This request expires {formatRelative(request.expires_at)} ({formatDateTime(request.expires_at)}).
        </p>

        <div className="mt-6 flex flex-wrap gap-2">
          <Button asChild variant="primary" size="lg">
            <a href={request.authorize_url}>Continue to {display}</a>
          </Button>
          <Button asChild size="lg">
            <Link to={`/connections/${id}`}>Back to the connection</Link>
          </Button>
        </div>

        {health.data?.mode === "mock" ? (
          <div className="mt-8 rounded-md border border-line bg-surface-2 p-4">
            <p className="text-sm font-medium">Mock provider</p>
            <p className="mt-1 text-sm text-ink-2">
              The gateway is in mock mode, so no real {display} is running. Approve here to complete the flow
              offline.
            </p>
            <Button className="mt-3" disabled={approve.isPending} onClick={() => approve.mutate()}>
              {approve.isPending ? "Approving" : "Approve in the mock provider"}
            </Button>
            {approve.error ? <p className="mt-2 text-xs text-bad">{errorMessage(approve.error)}</p> : null}
          </div>
        ) : null}

        <p className="mt-8 font-mono text-xs text-ink-3">
          connection {id}, state {shortId(request.state)}
        </p>
      </>
    );
  }

  return (
    <div className="min-h-dvh bg-canvas px-4 py-10 sm:py-16">
      <div className="mx-auto max-w-[36rem] rounded-lg border border-line bg-surface px-6 py-8 sm:px-10 sm:py-10">
        <p className="text-sm text-ink-2">{tenant?.name ?? ""}</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-[-0.01em]">Connect {display}</h1>
        <p className="mt-3 text-sm leading-6 text-ink-2">
          {display} will ask you to sign in and approve access for {tenantName}. The platform requests only the
          permissions its endpoints need. They are listed here so you can check them before you continue.
        </p>
        {body}
      </div>
    </div>
  );
}
