import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code";
import { Checkbox, Field, Select } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Status, Tag } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/api";
import { pluralize, secretRefs } from "@/lib/format";
import type { CallResult, Manifest } from "@/lib/types";
import { KeyValueFields, SecretFields, splitSecrets, type FieldSpec, type SecretSource } from "./key-value-fields";

export interface CallInput {
  endpoint_id: string;
  params: Record<string, string>;
  paginate: boolean;
  config: Record<string, string>;
  secrets: Record<string, string>;
  secret_env: Record<string, string>;
}

/**
 * Call one endpoint through the gateway and show what came back. In "direct" mode the caller
 * supplies connection config and secrets; in "connection" mode the vault supplies them.
 */
export function CallConsole({
  manifest,
  mode,
  disabledReason,
  onCall,
  controls,
}: {
  manifest: Manifest;
  mode: "direct" | "connection";
  disabledReason?: ReactNode;
  onCall: (input: CallInput) => Promise<CallResult>;
  controls?: ReactNode;
}) {
  const [endpointId, setEndpointId] = useState(manifest.endpoints[0]?.id ?? "");
  const [params, setParams] = useState<Record<string, string>>({});
  const [paginate, setPaginate] = useState(true);
  const [config, setConfig] = useState<Record<string, string>>({});
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [secretSources, setSecretSources] = useState<Record<string, SecretSource>>({});
  const call = useMutation({ mutationFn: onCall });

  const endpoint = manifest.endpoints.find((e) => e.id === endpointId) ?? manifest.endpoints[0];
  const refs = secretRefs(manifest.auth);
  const paramSpecs: FieldSpec[] = endpoint
    ? endpoint.parameters.map((p) => ({
        name: p.name,
        required: p.required || p.location === "path",
        description: p.description || `${p.location} parameter`,
      }))
    : [];

  if (disabledReason) return <Notice>{disabledReason}</Notice>;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!endpoint) return;
    call.mutate({
      endpoint_id: endpoint.id,
      params: Object.fromEntries(Object.entries(params).filter(([, value]) => value !== "")),
      paginate,
      config,
      ...splitSecrets(refs, secretValues, secretSources),
    });
  }

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
      <form onSubmit={submit} className="space-y-5">
        {controls}
        <Field label="Endpoint" htmlFor="call-endpoint" hint={endpoint?.description || undefined}>
          <Select
            id="call-endpoint"
            value={endpoint?.id ?? ""}
            onChange={(event) => {
              setEndpointId(event.target.value);
              setParams({});
            }}
          >
            {manifest.endpoints.map((e) => (
              <option key={e.id} value={e.id}>
                {e.id} ({e.method} {e.path})
              </option>
            ))}
          </Select>
        </Field>
        {paramSpecs.length ? (
          <fieldset className="space-y-3">
            <legend className="mb-2 text-xs font-medium text-ink-2">Parameters</legend>
            <KeyValueFields idPrefix="param" specs={paramSpecs} values={params} onChange={setParams} />
          </fieldset>
        ) : (
          <p className="text-xs text-ink-3">This endpoint takes no parameters.</p>
        )}
        {endpoint && endpoint.pagination.style !== "none" ? (
          <Checkbox
            label="Follow pagination"
            checked={paginate}
            onChange={(event) => setPaginate(event.target.checked)}
          />
        ) : null}
        {mode === "direct" && manifest.config_vars.length ? (
          <fieldset className="space-y-3">
            <legend className="mb-2 text-xs font-medium text-ink-2">Connection config</legend>
            <KeyValueFields
              idPrefix="config"
              specs={manifest.config_vars.map((name) => ({
                name,
                required: true,
                description: `Fills {${name}} in the base URL`,
              }))}
              values={config}
              onChange={setConfig}
            />
          </fieldset>
        ) : null}
        {mode === "direct" && refs.length ? (
          <fieldset>
            <legend className="mb-2 text-xs font-medium text-ink-2">Secrets</legend>
            <SecretFields
              idPrefix="secret"
              refs={refs}
              values={secretValues}
              sources={secretSources}
              onChange={(values, sources) => {
                setSecretValues(values);
                setSecretSources(sources);
              }}
            />
          </fieldset>
        ) : null}
        <Button type="submit" variant="primary" disabled={call.isPending || !endpoint}>
          {call.isPending ? "Calling" : `Call ${endpoint?.id ?? ""}`}
        </Button>
        {call.error ? (
          <Notice tone="bad" title="Call failed">
            {errorMessage(call.error)}
          </Notice>
        ) : null}
      </form>
      <div className="min-w-0">
        {call.data ? (
          <CallResultView result={call.data} />
        ) : (
          <div className="rounded-lg border border-line px-5 py-10 text-sm text-ink-3">
            Results appear here: the HTTP status, pages fetched, canonical records, and any drift the gateway
            noticed in the response.
          </div>
        )}
      </div>
    </div>
  );
}

export function CallResultView({ result }: { result: CallResult }) {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
        <Status tone={result.ok ? "ok" : "bad"} className="font-medium">
          {result.ok ? "Succeeded" : "Failed"}
        </Status>
        <span>HTTP {result.status_code ?? "none"}</span>
        <span>{pluralize(result.pages, "page")}</span>
        <span>{pluralize(result.records.length, "record")}</span>
        {result.canonical_object ? (
          <span>
            {result.canonical.length} {result.canonical_object}
          </span>
        ) : null}
      </div>
      {result.url ? <div className="font-mono text-xs break-all text-ink-2">{result.url}</div> : null}
      {result.drift_events.length ? (
        <Notice tone="bad" title={pluralize(result.drift_events.length, "drift event")}>
          <ul className="space-y-1">
            {result.drift_events.map((event, index) => (
              <li key={index} className="flex flex-wrap items-baseline gap-x-2">
                <Tag>{event.kind}</Tag>
                <span className="font-mono text-xs">{event.endpoint_id}</span>
                <span>{event.detail}</span>
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {result.validation_errors.length ? (
        <Notice tone="bad" title="Response did not match the schema">
          <ul className="space-y-0.5 font-mono text-xs">
            {result.validation_errors.map((error, index) => (
              <li key={index}>{error}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {result.mapping_errors.length ? (
        <Notice tone="warn" title="Mapping problems">
          <ul className="space-y-0.5 font-mono text-xs">
            {result.mapping_errors.map((error, index) => (
              <li key={index}>{error}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      <Tabs defaultValue={result.canonical.length ? "canonical" : "raw"}>
        <TabsList>
          <TabsTrigger value="canonical">Canonical {result.canonical_object ?? "objects"}</TabsTrigger>
          <TabsTrigger value="records">Records</TabsTrigger>
          <TabsTrigger value="raw">First page</TabsTrigger>
        </TabsList>
        <TabsContent value="canonical">
          {result.canonical.length ? (
            <CanonicalTable rows={result.canonical} />
          ) : (
            <p className="text-sm text-ink-3">
              {result.canonical_object
                ? "The mapping produced no canonical objects from these records."
                : "This endpoint has no mapping, so records pass through unchanged."}
            </p>
          )}
        </TabsContent>
        <TabsContent value="records">
          <CodeBlock value={result.records} />
        </TabsContent>
        <TabsContent value="raw">
          <CodeBlock value={result.raw_first_page} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

const preferredColumns = [
  "source_id",
  "display_name",
  "first_name",
  "last_name",
  "work_email",
  "job_title",
  "department",
  "employment_status",
  "hire_date",
  "name",
  "parent_source_id",
];

function cell(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "object") return <span className="font-mono text-xs">{JSON.stringify(value)}</span>;
  return String(value);
}

function CanonicalTable({ rows }: { rows: Record<string, unknown>[] }) {
  const present = new Set<string>();
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      if (key !== "source_integration" && value !== null && value !== undefined && value !== "") present.add(key);
    }
  }
  const columns = [
    ...preferredColumns.filter((column) => present.has(column)),
    ...[...present].filter((column) => !preferredColumns.includes(column)).sort(),
  ];
  const shown = rows.slice(0, 50);
  return (
    <div className="space-y-2">
      <Table>
        <THead>
          <TR>
            {columns.map((column) => (
              <TH key={column} className="font-mono font-normal">
                {column}
              </TH>
            ))}
          </TR>
        </THead>
        <TBody>
          {shown.map((row, index) => (
            <TR key={index}>
              {columns.map((column) => (
                <TD key={column} className="whitespace-nowrap">
                  {cell(row[column])}
                </TD>
              ))}
            </TR>
          ))}
        </TBody>
      </Table>
      {rows.length > shown.length ? (
        <p className="text-xs text-ink-3">
          Showing the first {shown.length} of {rows.length}.
        </p>
      ) : null}
    </div>
  );
}
