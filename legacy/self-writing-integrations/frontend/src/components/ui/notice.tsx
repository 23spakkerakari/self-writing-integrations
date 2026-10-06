import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

type NoticeTone = "info" | "ok" | "warn" | "bad";

const tones: Record<NoticeTone, string> = {
  info: "border-l-ink-3 bg-surface-2",
  ok: "border-l-ok bg-ok-soft",
  warn: "border-l-warn bg-warn-soft",
  bad: "border-l-bad bg-bad-soft",
};

export function Notice({
  tone = "info",
  title,
  children,
  className,
}: {
  tone?: NoticeTone;
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div
      role={tone === "bad" ? "alert" : undefined}
      className={cn("rounded-md border border-line border-l-2 px-3.5 py-2.5 text-sm", tones[tone], className)}
    >
      {title ? <div className="font-medium">{title}</div> : null}
      {children ? <div className={cn("whitespace-pre-line text-ink-2", title && "mt-0.5")}>{children}</div> : null}
    </div>
  );
}
