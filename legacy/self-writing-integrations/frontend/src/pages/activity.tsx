import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { AuditTimeline } from "@/components/audit-timeline";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/field";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState, Loading } from "@/components/ui/loading";
import { PageHeader } from "@/components/ui/page-header";
import { Status } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, notificationTone, shortId } from "@/lib/format";
import { useConnections, useIntegrationManifests } from "@/lib/hooks";
import { useTenant } from "@/lib/tenant";

export function ActivityPage() {
  const { tenant, tenantId, isLoading } = useTenant();
  const queryClient = useQueryClient();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const audit = useQuery({ queryKey: ["tenant-audit", tenantId], queryFn: () => api.audit(tenantId!), enabled: !!tenantId });
  const notifications = useQuery({
    queryKey: ["notifications", tenantId],
    queryFn: () => api.notifications.list(tenantId!),
    enabled: !!tenantId,
  });
  const connections = useConnections(tenantId);
  const manifests = useIntegrationManifests();
  const markRead = useMutation({
    mutationFn: (id: number) => api.notifications.markRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications", tenantId] }),
    onError: (error) => toast.error(errorMessage(error)),
  });

  const connectionLabel = (connectionId: string) => {
    const connection = connections.data?.find((c) => c.id === connectionId);
    const name = connection
      ? (manifests.current[connection.integration_name]?.manifest.display_name ?? connection.integration_name)
      : "connection";
    return (
      <Link className="hover:underline" to={`/connections/${connectionId}`}>
        {name} {shortId(connectionId)}
      </Link>
    );
  };

  const unread = notifications.data?.filter((n) => !n.read).length ?? 0;
  const shown = (notifications.data ?? []).filter((n) => !unreadOnly || !n.read).slice().reverse();

  return (
    <>
      <PageHeader
        title="Activity"
        meta={tenant ? <span>Auth events and notifications for {tenant.name}</span> : null}
      />
      {isLoading ? (
        <Loading />
      ) : !tenant ? (
        <EmptyState title="No tenant selected">
          <p>Create a tenant from the sidebar. Its connections' auth events and notifications show up here.</p>
        </EmptyState>
      ) : (
        <Tabs defaultValue="audit">
          <TabsList>
            <TabsTrigger value="audit">Audit log{audit.data ? ` (${audit.data.length})` : ""}</TabsTrigger>
            <TabsTrigger value="notifications">Notifications{unread ? ` (${unread} unread)` : ""}</TabsTrigger>
          </TabsList>
          <TabsContent value="audit">
            {audit.error ? (
              <ErrorState error={audit.error} retry={() => void audit.refetch()} />
            ) : !audit.data ? (
              <Loading />
            ) : audit.data.length === 0 ? (
              <EmptyState title="Nothing audited yet">
                <p>
                  Every credential event is appended here: consent, refreshes, failures, revocations. Create a
                  connection to see the first one.
                </p>
              </EmptyState>
            ) : (
              <AuditTimeline events={audit.data} connectionLabel={connectionLabel} />
            )}
          </TabsContent>
          <TabsContent value="notifications">
            {notifications.error ? (
              <ErrorState error={notifications.error} retry={() => void notifications.refetch()} />
            ) : !notifications.data ? (
              <Loading />
            ) : notifications.data.length === 0 ? (
              <EmptyState title="No notifications">
                <p>
                  The broker notifies a tenant when something needs a person, such as a provider rejecting a refresh
                  token. Silence here is good news.
                </p>
              </EmptyState>
            ) : (
              <div className="space-y-3">
                <Checkbox
                  label="Unread only"
                  checked={unreadOnly}
                  onChange={(event) => setUnreadOnly(event.target.checked)}
                />
                <ol className="divide-y divide-line border-t-[1.5px] border-t-ink">
                  {shown.map((note) => (
                    <li key={note.id} className="flex flex-wrap items-start gap-x-6 gap-y-2 px-4 py-3">
                      <time dateTime={note.created_at} className="w-44 shrink-0 font-mono text-xs text-ink-3 tabular-nums">
                        {formatDateTime(note.created_at)}
                      </time>
                      <div className="min-w-0 flex-1 space-y-1">
                        <Status tone={notificationTone(note.kind)} className={note.read ? "" : "font-medium"}>
                          {note.kind.replaceAll("_", " ")}
                        </Status>
                        <p className="text-sm text-ink-2">{note.message}</p>
                        {note.connection_id ? (
                          <p className="text-xs text-ink-3">{connectionLabel(note.connection_id)}</p>
                        ) : null}
                      </div>
                      {note.read ? (
                        <span className="text-xs text-ink-3">Read</span>
                      ) : (
                        <Button size="sm" disabled={markRead.isPending} onClick={() => markRead.mutate(note.id)}>
                          Mark as read
                        </Button>
                      )}
                    </li>
                  ))}
                  {shown.length === 0 ? <li className="px-4 py-3 text-sm text-ink-3">Everything is read.</li> : null}
                </ol>
              </div>
            )}
          </TabsContent>
        </Tabs>
      )}
    </>
  );
}
