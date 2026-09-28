import type { ReactNode } from "react";

export function EmptyState({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="rounded-lg border border-line px-6 py-8">
      <h2 className="text-base font-medium">{title}</h2>
      {children ? <div className="mt-1.5 max-w-prose space-y-3 text-sm leading-6 text-ink-2">{children}</div> : null}
      {actions ? <div className="mt-5 flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}
