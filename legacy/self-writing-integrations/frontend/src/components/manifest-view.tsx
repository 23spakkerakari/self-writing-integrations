import type { ReactNode } from "react";
import { CodeBlock } from "@/components/ui/code";
import { Facts, type Fact } from "@/components/ui/facts";
import { Section } from "@/components/ui/section";
import { Mono, Tag } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { pluralize } from "@/lib/format";
import type { Auth, Endpoint, Manifest, Mapping, Pagination } from "@/lib/types";

function describeAuth(auth: Auth): ReactNode {
  switch (auth.type) {
    case "none":
      return "No authentication";
    case "api_key":
      return (
        <>
          API key sent as the {auth.location} <Mono>{auth.name}</Mono>
          {auth.prefix ? (
            <>
              {" "}
              with prefix <Mono>{JSON.stringify(auth.prefix)}</Mono>
            </>
          ) : null}
          , from secret <Mono>{auth.secret_ref}</Mono>
        </>
      );
    case "bearer":
      return (
        <>
          Bearer token from secret <Mono>{auth.secret_ref}</Mono>
        </>
      );
    case "basic":
      return (
        <>
          HTTP Basic. Username{" "}
          {auth.username_secret_ref ? (
            <>
              from secret <Mono>{auth.username_secret_ref}</Mono>
            </>
          ) : (
            <>
              is the literal <Mono>{auth.username_literal}</Mono>
            </>
          )}
          , password{" "}
          {auth.password_secret_ref ? (
            <>
              from secret <Mono>{auth.password_secret_ref}</Mono>
            </>
          ) : (
            <>
              is the literal <Mono>{auth.password_literal}</Mono>
            </>
          )}
          .
        </>
      );
    case "oauth2":
      return (
        <div className="space-y-1">
          <div>
            OAuth 2.0 authorization code{auth.pkce ? " with PKCE" : ""}, token endpoint auth{" "}
            {auth.token_auth_method.replaceAll("_", " ")}
          </div>
          <div className="text-xs text-ink-2">
            Authorize at <Mono className="break-all">{auth.authorization_url}</Mono>
          </div>
          <div className="text-xs text-ink-2">
            Tokens from <Mono className="break-all">{auth.token_url}</Mono>
          </div>
          <div className="text-xs text-ink-2">Refresh {auth.refresh_leeway_seconds} s before expiry</div>
          {auth.scopes.length ? (
            <div className="flex flex-wrap gap-1 pt-1">
              {auth.scopes.map((scope) => (
                <Tag key={scope}>{scope}</Tag>
              ))}
            </div>
          ) : null}
        </div>
      );
  }
}

function describePagination(pagination: Pagination): string {
  switch (pagination.style) {
    case "none":
      return "None";
    case "page":
      return `Page numbers in ${pagination.page_param}, ${pagination.page_size} per page${
        pagination.size_param ? ` via ${pagination.size_param}` : ""
      }`;
    case "offset":
      return `Offset in ${pagination.offset_param}, limit in ${pagination.limit_param}, ${pagination.page_size} per page`;
    case "cursor":
      return `Cursor in ${pagination.cursor_param}, next cursor read from ${pagination.next_cursor_path ?? "the body"}`;
  }
}

function tags(entries: [string, string][]): ReactNode {
  return (
    <span className="flex flex-wrap gap-1">
      {entries.map(([key, value]) => (
        <Tag key={key}>{value ? `${key}: ${value}` : key}</Tag>
      ))}
    </span>
  );
}

export function ManifestView({ manifest }: { manifest: Manifest }) {
  const headers = Object.entries(manifest.default_headers);
  return (
    <div className="space-y-8">
      <Section title="Connection">
        <Facts
          items={[
            { label: "Base URL", value: <Mono className="break-all">{manifest.base_url}</Mono> },
            {
              label: "Config variables",
              value: manifest.config_vars.length ? tags(manifest.config_vars.map((v) => [v, ""])) : null,
            },
            { label: "Auth", value: describeAuth(manifest.auth) },
            { label: "Default headers", value: headers.length ? tags(headers) : null },
            {
              label: "Rate limit",
              value: `${manifest.rate_limit.requests_per_second} requests per second, burst of ${manifest.rate_limit.burst}`,
            },
            {
              label: "Retries",
              value: `Up to ${manifest.retry.max_attempts} attempts, ${manifest.retry.backoff_seconds} s backoff, on HTTP ${manifest.retry.retry_on_status.join(", ")}`,
            },
          ]}
        />
      </Section>
      <Section title="Endpoints" description={pluralize(manifest.endpoints.length, "endpoint")}>
        <div className="space-y-4">
          {manifest.endpoints.map((endpoint) => (
            <EndpointCard
              key={endpoint.id}
              endpoint={endpoint}
              mapping={manifest.mappings.find((mapping) => mapping.endpoint_id === endpoint.id)}
            />
          ))}
        </div>
      </Section>
    </div>
  );
}

function EndpointCard({ endpoint, mapping }: { endpoint: Endpoint; mapping: Mapping | undefined }) {
  const facts: Fact[] = [
    { label: "Pagination", value: describePagination(endpoint.pagination) },
    { label: "Records at", value: endpoint.items_path ? <Mono>{endpoint.items_path}</Mono> : "The body itself" },
  ];
  if (endpoint.scopes.length) facts.push({ label: "Scopes", value: tags(endpoint.scopes.map((s) => [s, ""])) });
  const defaultQuery = Object.entries(endpoint.default_query);
  if (defaultQuery.length) facts.push({ label: "Default query", value: tags(defaultQuery) });

  return (
    <article className="rounded-lg border border-line">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line bg-surface-2 px-4 py-2.5">
        <Mono className="font-medium">{endpoint.id}</Mono>
        <span className="font-mono text-xs text-ink-2">
          {endpoint.method} {endpoint.path}
        </span>
        {mapping ? (
          <span className="ml-auto text-xs text-ink-2">Maps to {mapping.canonical_object}</span>
        ) : (
          <span className="ml-auto text-xs text-ink-3">No mapping, records pass through</span>
        )}
      </header>
      <div className="space-y-4 px-4 py-3">
        {endpoint.description ? <p className="text-sm text-ink-2">{endpoint.description}</p> : null}
        <Facts items={facts} />
        {endpoint.parameters.length ? (
          <Table>
            <THead>
              <TR>
                <TH>Parameter</TH>
                <TH>In</TH>
                <TH>Required</TH>
                <TH>Description</TH>
              </TR>
            </THead>
            <TBody>
              {endpoint.parameters.map((parameter) => (
                <TR key={parameter.name}>
                  <TD>
                    <Mono>{parameter.name}</Mono>
                  </TD>
                  <TD>{parameter.location}</TD>
                  <TD>{parameter.required || parameter.location === "path" ? "Yes" : "No"}</TD>
                  <TD className="text-ink-2">{parameter.description}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        ) : null}
        {mapping ? <MappingTable mapping={mapping} /> : null}
        {endpoint.response_schema ? (
          <details>
            <summary className="text-xs font-medium text-ink-2 hover:text-ink">Response schema</summary>
            <CodeBlock className="mt-2" value={endpoint.response_schema} maxHeight="20rem" />
          </details>
        ) : null}
      </div>
    </article>
  );
}

function MappingTable({ mapping }: { mapping: Mapping }) {
  return (
    <Table>
      <THead>
        <TR>
          <TH>{mapping.canonical_object} field</TH>
          <TH>Source path</TH>
          <TH>Transform</TH>
        </TR>
      </THead>
      <TBody>
        {mapping.fields.map((field) => (
          <TR key={field.target}>
            <TD>
              <Mono>{field.target}</Mono>
            </TD>
            <TD>{field.source ? <Mono>{field.source}</Mono> : <span className="text-ink-3">None</span>}</TD>
            <TD>
              {field.transform ? (
                <span className="flex flex-wrap items-center gap-1.5">
                  <Tag>{field.transform}</Tag>
                  {Object.keys(field.args).length ? (
                    <span className="font-mono text-xs text-ink-2">{JSON.stringify(field.args)}</span>
                  ) : null}
                </span>
              ) : (
                <span className="text-ink-3">None</span>
              )}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}
