import { Loader2 } from "lucide-react";
import { errorMessage } from "@/lib/api";
import { Button } from "./button";
import { Notice } from "./notice";

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm text-ink-3" role="status">
      <Loader2 className="size-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <Notice tone="bad" title="Could not load this page">
      {errorMessage(error)}
      {retry ? (
        <div className="mt-2">
          <Button size="sm" onClick={retry}>
            Try again
          </Button>
        </div>
      ) : null}
    </Notice>
  );
}
