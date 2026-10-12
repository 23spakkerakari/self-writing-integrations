import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "sonner";
import { Shell } from "@/components/shell/shell";
import { TenantProvider } from "@/lib/tenant";
import { ActivityPage } from "@/pages/activity";
import { CanonicalPage } from "@/pages/canonical";
import { ChangePage } from "@/pages/change-detail";
import { ConnectionPage } from "@/pages/connection-detail";
import { ConnectionsPage } from "@/pages/connections";
import { ConsentPage } from "@/pages/consent";
import { DriftPage } from "@/pages/drift";
import { IncidentPage } from "@/pages/incident-detail";
import { IntegrationPage } from "@/pages/integration-detail";
import { IntegrationsPage } from "@/pages/integrations";
import { NewIntegrationPage } from "@/pages/new-integration";
import { NotFoundPage } from "@/pages/not-found";
import { VersionPage } from "@/pages/version-detail";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 } },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <TenantProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<Navigate to="/integrations" replace />} />
              <Route path="integrations" element={<IntegrationsPage />} />
              <Route path="integrations/new" element={<NewIntegrationPage />} />
              <Route path="integrations/:name" element={<IntegrationPage />} />
              <Route path="integrations/:name/versions/:version" element={<VersionPage />} />
              <Route path="connections" element={<ConnectionsPage />} />
              <Route path="connections/:id" element={<ConnectionPage />} />
              <Route path="drift" element={<DriftPage />} />
              <Route path="drift/incidents/:id" element={<IncidentPage />} />
              <Route path="changes/:id" element={<ChangePage />} />
              <Route path="activity" element={<ActivityPage />} />
              <Route path="canonical" element={<CanonicalPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
            <Route path="connections/:id/consent" element={<ConsentPage />} />
          </Routes>
        </TenantProvider>
      </BrowserRouter>
      <Toaster
        position="bottom-right"
        toastOptions={{ classNames: { toast: "font-sans text-sm", description: "text-ink-2" } }}
      />
    </QueryClientProvider>
  );
}
