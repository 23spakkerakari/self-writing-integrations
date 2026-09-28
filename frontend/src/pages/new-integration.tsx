import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Field, Input, Textarea } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage, validationIssues } from "@/lib/api";
import { pluralize } from "@/lib/format";
import type { PipelineResult } from "@/lib/types";

const TEMPLATE = `name: example
version: 0.1.0
display_name: Example HR
description: Employee reads from the Example API.
base_url: "https://api.example.com/v1"
auth:
  type: api_key
  location: header
  name: X-Api-Key
  secret_ref: api_key
endpoints:
  - id: list_employees
    method: GET
    path: /employees
    description: All employees
    items_path: data
    pagination: { style: page, page_param: page, size_param: per_page, page_size: 100 }
    response_schema:
      type: object
      required: [data]
      properties:
        data:
          type: array
          items:
            type: object
            required: [id]
            properties:
              id: { type: string }
              name: { type: string }
              email: { type: [string, "null"] }
mappings:
  - endpoint_id: list_employees
    canonical_object: Employee
    fields:
      - { target: source_id, source: id }
      - { target: display_name, source: name }
      - { target: work_email, source: email }
`;

export function NewIntegrationPage() {
  const [search, setSearch] = useSearchParams();
  const tab = search.get("tab") === "synthesize" ? "synthesize" : "import";
  return (
    <>
      <PageHeader
        back={{ to: "/integrations", label: "Integrations" }}
        title="Add integration"
        meta={
          <span>
            An integration is a manifest: a declarative description of an API that the runtime interprets.
          </span>
        }
      />
      <Tabs value={tab} onValueChange={(value) => setSearch({ tab: value }, { replace: true })}>
        <TabsList>
          <TabsTrigger value="import">Import a manifest</TabsTrigger>
          <TabsTrigger value="synthesize">Synthesize from a spec</TabsTrigger>
        </TabsList>
        <TabsContent value="import">
          <ImportForm />
        </TabsContent>
        <TabsContent value="synthesize">
          <SynthesizeForm initialName={search.get("name") ?? ""} />
        </TabsContent>
      </Tabs>
    </>
  );
}

function ImportForm() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [provenance, setProvenance] = useState("manual");
  const [parseError, setParseError] = useState<string | null>(null);

  const importManifest = useMutation({
    mutationFn: (manifest: Record<string, unknown>) => api.integrations.import(manifest, provenance.trim() || "manual"),
    onSuccess: async (record) => {
      await queryClient.invalidateQueries({ queryKey: ["integrations"] });
      await queryClient.invalidateQueries({ queryKey: ["versions", record.name] });
      toast.success(`Imported ${record.name} ${record.version} as a draft`);
      navigate(`/integrations/${record.name}/versions/${record.version}`);
    },
  });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setParseError(null);
    let parsed: unknown;
    try {
      const YAML = await import("yaml");
      parsed = YAML.parse(text);
    } catch (error) {
      setParseError(`Could not parse this as YAML or JSON. ${errorMessage(error)}`);
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      setParseError("The manifest must be one object with name, base_url, auth and endpoints.");
      return;
    }
    importManifest.mutate(parsed as Record<string, unknown>);
  }

  const issues = validationIssues(importManifest.error);

  return (
    <form onSubmit={(event) => void submit(event)} className="max-w-3xl space-y-5">
      <Field
        label="Manifest"
        htmlFor="manifest-text"
        hint="YAML or JSON. The reference manifests in backend/manifests are a good first import."
      >
        <Textarea
          id="manifest-text"
          mono
          rows={22}
          spellCheck={false}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={"name: example\nversion: 0.1.0\ndisplay_name: Example\nbase_url: https://api.example.com\nauth: { type: none }\nendpoints: [...]"}
        />
      </Field>
      <div className="flex flex-wrap items-end gap-4">
        <Field
          label="Provenance"
          htmlFor="provenance"
          hint="Recorded on the version, for example manual or postman-export"
          className="w-64"
        >
          <Input id="provenance" value={provenance} onChange={(event) => setProvenance(event.target.value)} />
        </Field>
        <Button onClick={() => setText(TEMPLATE)}>Start from a template</Button>
      </div>
      {parseError ? <Notice tone="bad">{parseError}</Notice> : null}
      {importManifest.error && !issues.length ? (
        <Notice tone="bad" title="Import failed">
          {errorMessage(importManifest.error)}
        </Notice>
      ) : null}
      {issues.length ? (
        <Notice tone="bad" title="The manifest did not validate">
          <ul className="list-disc space-y-0.5 pl-4 font-mono text-xs">
            {issues.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      <Button type="submit" variant="primary" disabled={!text.trim() || importManifest.isPending}>
        {importManifest.isPending ? "Importing" : "Import as draft"}
      </Button>
    </form>
  );
}

const SLUG = /^[a-z0-9][a-z0-9_-]{1,63}$/;

function SynthesizeForm({ initialName }: { initialName: string }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(initialName);
  const [spec, setSpec] = useState("");
  const [rounds, setRounds] = useState(2);

  const synthesize = useMutation({
    mutationFn: () => api.integrations.synthesize({ name: name.trim(), spec_text: spec, max_rounds: rounds }),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["integrations"] });
      await queryClient.invalidateQueries({ queryKey: ["versions", result.record.name] });
      toast.success(`Stored ${result.record.name} ${result.record.version} as ${result.record.status}`);
    },
  });

  const slugOk = SLUG.test(name);

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        synthesize.mutate();
      }}
      className="max-w-3xl space-y-5"
    >
      <Notice>
        Runs the synthesis agent on the API host, which needs ANTHROPIC_API_KEY. Each round verifies the manifest
        against a mock built from the spec and feeds failures back to the agent. The result is stored as verified or
        rejected, never published.
      </Notice>
      <div className="grid gap-4 sm:grid-cols-[16rem_10rem]">
        <Field
          label="Integration name"
          htmlFor="synth-name"
          hint="Lowercase slug, used everywhere as the identifier"
          error={name && !slugOk ? "Use lowercase letters, digits, hyphens or underscores." : undefined}
        >
          <Input
            id="synth-name"
            className="font-mono text-[13px]"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="bamboohr"
          />
        </Field>
        <Field label="Repair rounds" htmlFor="synth-rounds" hint="1 to 5">
          <Input
            id="synth-rounds"
            type="number"
            min={1}
            max={5}
            value={rounds}
            onChange={(event) => setRounds(Math.min(5, Math.max(1, Number(event.target.value) || 1)))}
          />
        </Field>
      </div>
      <Field label="Spec" htmlFor="synth-spec" hint="OpenAPI YAML or JSON, documentation text, or captured traffic">
        <Textarea
          id="synth-spec"
          mono
          rows={18}
          spellCheck={false}
          value={spec}
          onChange={(event) => setSpec(event.target.value)}
        />
      </Field>
      {synthesize.error ? (
        <Notice tone="bad" title="Synthesis failed">
          {errorMessage(synthesize.error)}
        </Notice>
      ) : null}
      {synthesize.data ? <SynthesisResult result={synthesize.data} /> : null}
      <Button type="submit" variant="primary" disabled={!slugOk || !spec.trim() || synthesize.isPending}>
        {synthesize.isPending ? "Synthesizing, this can take a minute or two" : "Synthesize and verify"}
      </Button>
    </form>
  );
}

function SynthesisResult({ result }: { result: PipelineResult }) {
  return (
    <Notice
      tone={result.report.passed ? "ok" : "warn"}
      title={`${result.record.name} ${result.record.version} is ${result.record.status}`}
    >
      {pluralize(result.rounds, "round")}, {pluralize(result.model_attempts, "model attempt")}. Verification{" "}
      {result.report.passed ? "passed" : "failed"} on mock.{" "}
      <Link className="underline" to={`/integrations/${result.record.name}/versions/${result.record.version}`}>
        Open the version
      </Link>
    </Notice>
  );
}
