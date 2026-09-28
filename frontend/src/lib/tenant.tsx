import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import type { Tenant } from "./types";

interface TenantContextValue {
  tenants: Tenant[];
  tenant: Tenant | null;
  tenantId: string | null;
  setTenantId: (id: string) => void;
  isLoading: boolean;
  error: unknown;
}

const TenantContext = createContext<TenantContextValue | null>(null);
const STORAGE_KEY = "swi.tenant";

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function TenantProvider({ children }: { children: ReactNode }) {
  const query = useQuery({ queryKey: ["tenants"], queryFn: api.tenants.list });
  const [stored, setStored] = useState<string | null>(readStored);
  const tenants = useMemo(() => query.data ?? [], [query.data]);

  const value = useMemo<TenantContextValue>(() => {
    const tenantId = stored && tenants.some((t) => t.id === stored) ? stored : (tenants[0]?.id ?? null);
    return {
      tenants,
      tenant: tenants.find((t) => t.id === tenantId) ?? null,
      tenantId,
      setTenantId: (id: string) => {
        setStored(id);
        try {
          localStorage.setItem(STORAGE_KEY, id);
        } catch {
          // Storage can be unavailable; the selection still works for this session.
        }
      },
      isLoading: query.isLoading,
      error: query.error,
    };
  }, [tenants, stored, query.isLoading, query.error]);

  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

export function useTenant(): TenantContextValue {
  const value = useContext(TenantContext);
  if (!value) throw new Error("useTenant must be used inside TenantProvider");
  return value;
}
