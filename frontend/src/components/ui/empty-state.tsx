import type { ReactNode } from "react";

export function EmptyState({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="border-t-[1.5px] border-ink pt-5 pb-2">
      <h2 className="font-display text-[17px] font-semibold">{title}</h2>
      {children ? <div className="mt-1.5 max-w-prose space-y-3 text-sm leading-6 text-ink-2">{children}</div> : null}
      {actions ? <div className="mt-5 flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}
