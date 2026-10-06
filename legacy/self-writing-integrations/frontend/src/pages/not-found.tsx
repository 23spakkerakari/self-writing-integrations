import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        title="There is nothing at this address"
        actions={
          <Button asChild variant="primary">
            <Link to="/integrations">Go to integrations</Link>
          </Button>
        }
      >
        <p>The link may be out of date, or the integration or connection it pointed at was removed.</p>
      </EmptyState>
    </>
  );
}
