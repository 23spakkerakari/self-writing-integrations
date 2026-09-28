import { Fragment, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface Fact {
  label: string;
  value: ReactNode;
}

/** A key/value readout. Empty values print as "None" so a missing fact is visible, not silent. */
export function Facts({ items, className }: { items: Fact[]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-2 text-sm", className)}>
      {items.map((fact) => (
        <Fragment key={fact.label}>
          <dt className="text-ink-3">{fact.label}</dt>
          <dd className="min-w-0 break-words">
            {fact.value === null || fact.value === undefined || fact.value === "" ? (
              <span className="text-ink-3">None</span>
            ) : (
              fact.value
            )}
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}
