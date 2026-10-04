import { ChevronLeft } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/cn";

export interface BlockCell {
  label: string;
  value: ReactNode;
  /** Let this cell take the remaining width; use for prose such as a description. */
  grow?: boolean;
}

/**
 * The title block of a page. A heavy rule opens the sheet, the title and its actions sit on the
 * first row, and block cells record the facts a reader checks before acting: status, version,
 * who, when. List pages pass meta instead and get a single line under the title.
 */
export function PageHeader({
  title,
  meta,
  block,
  actions,
  back,
}: {
  title: ReactNode;
  meta?: ReactNode;
  block?: BlockCell[];
  actions?: ReactNode;
  back?: { to: string; label: string };
}) {
  return (
    <header className="mb-6">
      {back ? (
        <Link to={back.to} className="mb-3 inline-flex items-center gap-0.5 text-xs text-ink-2 hover:text-ink">
          <ChevronLeft className="size-3.5" />
          {back.label}
        </Link>
      ) : null}
      <div className="border-t-2 border-ink pt-3">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <h1 className="min-w-0 flex-1 basis-80 font-display text-[26px] leading-8 font-semibold tracking-[-0.01em]">
            {title}
          </h1>
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
        {block ? (
          <dl className="mt-4 flex flex-wrap gap-y-3">
            {block.map((cell, index) => (
              <div
                key={cell.label}
                className={cn(
                  "min-w-[9rem] py-0.5",
                  index === 0 ? "pr-3" : "border-l border-line px-3",
                  cell.grow && "min-w-[16rem] flex-1 basis-64",
                )}
              >
                <dt className="font-display text-[11px] leading-4 font-medium text-ink-3">{cell.label}</dt>
                <dd className="mt-0.5 min-w-0 text-sm break-words">
                  {cell.value === null || cell.value === undefined || cell.value === "" ? (
                    <span className="text-ink-3">None</span>
                  ) : (
                    cell.value
                  )}
                </dd>
              </div>
            ))}
          </dl>
        ) : meta ? (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-ink-2">{meta}</div>
        ) : null}
        <div className="mt-4 border-b border-line" />
      </div>
    </header>
  );
}
