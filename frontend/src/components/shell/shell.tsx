import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Activity, Database, Layers, Plug, Plus, Radar } from "lucide-react";
import { useState, type FormEvent } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/field";
import { Status } from "@/components/ui/status";
import { api, apiBase, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useHealth } from "@/lib/hooks";
import { useTenant } from "@/lib/tenant";

const nav = [
  { to: "/integrations", label: "Integrations", icon: Layers },
  { to: "/connections", label: "Connections", icon: Plug },
  { to: "/drift", label: "Drift", icon: Radar },
  { to: "/activity", label: "Activity", icon: Activity },
  { to: "/canonical", label: "Canonical model", icon: Database },
];

export function Shell() {
  return (
    <div className="min-h-dvh bg-surface lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
      <Sidebar />
      <main className="min-w-0 px-4 py-6 sm:px-6 lg:px-10 lg:py-8">
        <div className="mx-auto w-full max-w-[1200px]">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

function Sidebar() {
  return (
    <aside className="flex h-14 items-center gap-6 overflow-x-auto border-b border-line bg-canvas px-4 lg:sticky lg:top-0 lg:h-dvh lg:flex-col lg:items-stretch lg:gap-0 lg:overflow-visible lg:border-r lg:border-b-0 lg:px-4 lg:py-5">
      <div className="shrink-0">
        <div className="text-sm leading-tight font-medium">
          Self-writing
          <br className="hidden lg:block" /> integrations
        </div>
        <div className="hidden text-xs text-ink-3 lg:block">Developer console</div>
      </div>
      <nav aria-label="Main" className="flex items-center gap-1 lg:mt-7 lg:flex-col lg:items-stretch">
        {nav.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm whitespace-nowrap",
                isActive ? "bg-ink/6 font-medium text-ink" : "text-ink-2 hover:bg-ink/4 hover:text-ink",
              )
            }
          >
            <item.icon className="size-4 shrink-0" aria-hidden />
            {item.label}
          </NavLink>
        ))}
      </nav>
      <div className="ml-auto flex shrink-0 items-center gap-4 lg:mt-auto lg:ml-0 lg:flex-col lg:items-stretch lg:gap-3">
        <TenantSwitcher />
        <ModeIndicator />
      </div>
    </aside>
  );
}

function TenantSwitcher() {
  const { tenants, tenantId, setTenantId, isLoading } = useTenant();
  const [open, setOpen] = useState(false);
  return (
    <div className="flex items-center gap-1.5 lg:flex-col lg:items-stretch lg:gap-1">
      <label className="hidden text-xs text-ink-3 lg:block" htmlFor="tenant-select">
        Tenant
      </label>
      <div className="flex items-center gap-1">
        <Select
          id="tenant-select"
          aria-label="Tenant"
          value={tenantId ?? ""}
          onChange={(event) => setTenantId(event.target.value)}
          disabled={!tenants.length}
          className="min-w-32 lg:min-w-0 lg:flex-1"
        >
          {tenants.length ? (
            tenants.map((tenant) => (
              <option key={tenant.id} value={tenant.id}>
                {tenant.name}
              </option>
            ))
          ) : (
            <option value="">{isLoading ? "Loading" : "No tenants"}</option>
          )}
        </Select>
        <Button size="icon" variant="ghost" aria-label="New tenant" title="New tenant" onClick={() => setOpen(true)}>
          <Plus />
        </Button>
      </div>
      <NewTenantDialog open={open} onOpenChange={setOpen} />
    </div>
  );
}

export function NewTenantDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [name, setName] = useState("");
  const queryClient = useQueryClient();
  const { setTenantId } = useTenant();
  const create = useMutation({
    mutationFn: (value: string) => api.tenants.create(value),
    onSuccess: async (tenant) => {
      await queryClient.invalidateQueries({ queryKey: ["tenants"] });
      setTenantId(tenant.id);
      toast.success(`Created tenant ${tenant.name}`);
      setName("");
      onOpenChange(false);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>New tenant</DialogTitle>
            <DialogDescription>
              A tenant owns connections, credentials and audit history. Each one gets its own encryption key in
              the vault.
            </DialogDescription>
          </DialogHeader>
          <Field label="Name" htmlFor="tenant-name">
            <Input
              id="tenant-name"
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Acme"
            />
          </Field>
          {create.error ? <p className="mt-2 text-xs text-bad">{errorMessage(create.error)}</p> : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!name.trim() || create.isPending}>
              {create.isPending ? "Creating" : "Create tenant"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ModeIndicator() {
  const health = useHealth();
  const title = `API at ${apiBase}`;
  if (health.error) {
    return (
      <Status tone="bad" className="text-xs" title={title}>
        API unreachable
      </Status>
    );
  }
  if (!health.data) {
    return (
      <Status tone="neutral" className="text-xs" title={title}>
        Checking API
      </Status>
    );
  }
  return (
    <Status tone={health.data.mode === "mock" ? "warn" : "ok"} className="text-xs" title={title}>
      {health.data.mode === "mock" ? "Mock gateway" : "Live gateway"}
    </Status>
  );
}
