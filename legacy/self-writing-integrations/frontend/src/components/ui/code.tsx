import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { cn } from "@/lib/cn";

export function CodeBlock({
  value,
  className,
  maxHeight = "24rem",
}: {
  value: unknown;
  className?: string;
  maxHeight?: string;
}) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Could not copy to the clipboard");
    }
  }

  return (
    <div className={cn("relative rounded-lg border border-line bg-surface-2", className)}>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy"}
        className="absolute top-2 right-2 rounded-md border border-line bg-surface p-1 text-ink-3 hover:text-ink"
      >
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
      <pre className="overflow-auto p-3 pr-10 font-mono text-xs leading-5 text-ink" style={{ maxHeight }}>
        <code>{text}</code>
      </pre>
    </div>
  );
}
