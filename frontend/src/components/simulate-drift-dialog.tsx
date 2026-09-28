import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { api, errorMessage } from "@/lib/api";
import type { DriftMutation, Manifest } from "@/lib/types";

const presets = [
  { type: "rename_field", label: "Rename a response field" },
  { type: "remove_field", label: "Remove a response field" },
  { type: "wrap_items", label: "Wrap the records in a new key" },
  { type: "status", label: "Return an error status" },
  { type: "non_json", label: "Return a non-JSON body" },
  { type: "sunset", label: "Announce a sunset date" },
  { type: "path_moved", label: "Move the endpoint to a new path" },
  { type: "endless_pagination", label: "Never finish paginating" },
] as const;

type PresetType = (typeof presets)[number]["type"];

/**
 * Mock mode only. Pins the mock provider to the published manifest plus one mutation so the next
 * call drifts for real, opens an incident, and gives the repair pipeline something to fix.
 */
export function SimulateDriftDialog({
  open,
  onOpenChange,
  name,
  manifest,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  name: string;
  manifest: Manifest;
}) {
  const queryClient = useQueryClient();
  const [type, setType] = useState<PresetType>("rename_field");
  const [endpointId, setEndpointId] = useState(manifest.endpoints[0]?.id ?? "");
  const [values, setValues] = useState<Record<string, string>>({ status_code: "500" });
  const preset = presets.find((p) => p.type === type)!;

  const inject = useMutation({
    mutationFn: () => {
      const mutation: DriftMutation = { type, endpoint_id: endpointId };
      if (type === "rename_field") Object.assign(mutation, { old: values.old ?? "", new: values.new ?? "" });
      if (type === "remove_field") mutation.field = values.field ?? "";
      if (type === "wrap_items") mutation.key = values.key ?? "data";
      if (type === "status") mutation.status_code = Number(values.status_code) || 500;
      if (type === "sunset" && values.successor) mutation.successor = values.successor;
      if (type === "path_moved") mutation.new_path = values.new_path ?? "";
      return api.mock.injectDrift(name, { name: preset.label, mutations: [mutation] });
    },
    onSuccess: async (world) => {
      await queryClient.invalidateQueries({ queryKey: ["drift-worlds"] });
      toast.message(`The mock provider now drifts from ${world.pinned_version}`, {
        description: "Call the endpoint to open an incident, then repair it from the Drift page.",
      });
      onOpenChange(false);
    },
  });

  const set = (key: string) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setValues({ ...values, [key]: event.target.value });

  function submit(event: FormEvent) {
    event.preventDefault();
    inject.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Simulate drift at the provider</DialogTitle>
            <DialogDescription>
              The mock keeps serving the published shape of {manifest.display_name} plus this change, to every
              caller and every manifest version. That is what a repair is verified against.
            </DialogDescription>
          </DialogHeader>
          <Field label="What changes" htmlFor="drift-type">
            <Select id="drift-type" value={type} onChange={(event) => setType(event.target.value as PresetType)}>
              {presets.map((p) => (
                <option key={p.type} value={p.type}>
                  {p.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Endpoint" htmlFor="drift-endpoint">
            <Select id="drift-endpoint" value={endpointId} onChange={(event) => setEndpointId(event.target.value)}>
              {manifest.endpoints.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.id} ({e.method} {e.path})
                </option>
              ))}
            </Select>
          </Field>
          {type === "rename_field" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Field today" htmlFor="drift-old">
                <Input id="drift-old" className="font-mono text-[13px]" value={values.old ?? ""} onChange={set("old")} placeholder="displayName" />
              </Field>
              <Field label="New name" htmlFor="drift-new">
                <Input id="drift-new" className="font-mono text-[13px]" value={values.new ?? ""} onChange={set("new")} placeholder="display_name" />
              </Field>
            </div>
          ) : null}
          {type === "remove_field" ? (
            <Field label="Field to remove" htmlFor="drift-field">
              <Input id="drift-field" className="font-mono text-[13px]" value={values.field ?? ""} onChange={set("field")} />
            </Field>
          ) : null}
          {type === "wrap_items" ? (
            <Field label="Wrapper key" htmlFor="drift-key">
              <Input id="drift-key" className="font-mono text-[13px]" value={values.key ?? "data"} onChange={set("key")} />
            </Field>
          ) : null}
          {type === "status" ? (
            <Field label="Status code" htmlFor="drift-status">
              <Input id="drift-status" type="number" min={100} max={599} value={values.status_code ?? "500"} onChange={set("status_code")} />
            </Field>
          ) : null}
          {type === "sunset" ? (
            <Field label="Successor path" htmlFor="drift-successor" hint="Optional. Sent as the Link successor header.">
              <Input id="drift-successor" className="font-mono text-[13px]" value={values.successor ?? ""} onChange={set("successor")} placeholder="/v2/employees" />
            </Field>
          ) : null}
          {type === "path_moved" ? (
            <Field label="New path" htmlFor="drift-path">
              <Input id="drift-path" className="font-mono text-[13px]" value={values.new_path ?? ""} onChange={set("new_path")} placeholder="/v2/employees/directory" />
            </Field>
          ) : null}
          {inject.error ? <Notice tone="bad">{errorMessage(inject.error)}</Notice> : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!endpointId || inject.isPending}>
              {inject.isPending ? "Applying" : "Apply drift"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
