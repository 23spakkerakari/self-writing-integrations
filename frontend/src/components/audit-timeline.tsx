import type { ReactNode } from "react";
import { Status, Tag } from "@/components/ui/status";
import { eventLabel, eventTone, formatDateTime } from "@/lib/format";
import type { AuthEvent } from "@/lib/types";

/** The append-only auth history, newest first. */
export function AuditTimeline({
  events,
  connectionLabel,
}: {
  events: AuthEvent[];
  connectionLabel?: (connectionId: string) => ReactNode;
}) {
  if (!events.length) return <p className="text-sm text-ink-3">No events yet.</p>;
  const ordered = [...events].sort((a, b) => b.id - a.id);
  return (
    <ol className="divide-y divide-line rounded-lg border border-line bg-surface">
      {ordered.map((event) => (
        <li key={event.id} className="grid gap-x-6 gap-y-1 px-4 py-3 sm:grid-cols-[11rem_minmax(0,1fr)]">
          <time dateTime={event.at} className="font-mono text-xs text-ink-3 tabular-nums">
            {formatDateTime(event.at)}
          </time>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <Status tone={eventTone(event.event)} className="font-medium">
                {eventLabel(event.event)}
              </Status>
              <span className="text-xs text-ink-3">by {event.actor}</span>
              {connectionLabel && event.connection_id ? (
                <span className="text-xs text-ink-3">{connectionLabel(event.connection_id)}</span>
              ) : null}
            </div>
            {event.scopes.length ? (
              <div className="flex flex-wrap gap-1">
                {event.scopes.map((scope) => (
                  <Tag key={scope}>{scope}</Tag>
                ))}
              </div>
            ) : null}
            {event.detail ? <p className="text-xs text-ink-2">{event.detail}</p> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}
