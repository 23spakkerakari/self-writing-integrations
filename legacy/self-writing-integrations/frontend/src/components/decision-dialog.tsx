import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, Input, Textarea } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { errorMessage } from "@/lib/api";
import type { Decision } from "@/lib/types";

/** A human decision that gets recorded: who decided, and a note. Used for approve, reject, dismiss, abort and rollback. */
export function DecisionDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  variant = "primary",
  extra,
  pending,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  variant?: "primary" | "danger";
  extra?: ReactNode;
  pending?: boolean;
  error?: unknown;
  onConfirm: (decision: Decision) => void;
}) {
  const [actor, setActor] = useState("human");
  const [note, setNote] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    onConfirm({ actor: actor.trim() || "human", note: note.trim() });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description ? <DialogDescription>{description}</DialogDescription> : null}
          </DialogHeader>
          <Field label="Decided by" htmlFor="decision-actor" hint="Recorded on the change request and in the audit trail">
            <Input id="decision-actor" value={actor} onChange={(event) => setActor(event.target.value)} />
          </Field>
          <Field label="Note" htmlFor="decision-note">
            <Textarea id="decision-note" rows={3} value={note} onChange={(event) => setNote(event.target.value)} />
          </Field>
          {extra}
          {error ? <Notice tone="bad">{errorMessage(error)}</Notice> : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant={variant} disabled={pending}>
              {confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
