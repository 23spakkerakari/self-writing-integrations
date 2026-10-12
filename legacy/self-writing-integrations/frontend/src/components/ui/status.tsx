import type * as React from "react";
import { cn } from "@/lib/cn";
import type { Tone } from "@/lib/format";

const marker: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  idle: "bg-idle",
  neutral: "bg-line-strong",
};

/** A state readout: a small coloured square and a plain-language label. Colour is reserved for state. */
export function Status({
  tone,
  children,
  className,
  title,
}: {
  tone: Tone;
  children: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)} title={title}>
      <span aria-hidden className={cn("inline-block size-1.5 shrink-0 rounded-none", marker[tone])} />
      {children}
    </span>
  );
}

/** An identifier chip: scopes, drift kinds, transform names. */
export function Tag({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <code
      className={cn(
        "inline-block rounded-sm border border-line bg-surface-2 px-1.5 py-px font-mono text-xs leading-5 text-ink-2",
        className,
      )}
    >
      {children}
    </code>
  );
}

export function Mono({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn("font-mono text-[13px]", className)}>{children}</span>;
}
