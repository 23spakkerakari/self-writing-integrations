import { useQuery } from "@tanstack/react-query";
import { Integration, IntegrationCard } from "@/components/ui/integration-card";
import { ErrorState, Loading } from "@/components/ui/loading";
import { PageHeader } from "@/components/ui/page-header";
import { Section } from "@/components/ui/section";
import { Mono } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { api, apiBase } from "@/lib/api";
import { schemaFields } from "@/lib/schema";

const identityFields = ["display_name", "work_email", "first_name", "last_name"];

export function CanonicalPage() {
  const reference = useQuery({ queryKey: ["canonical"], queryFn: api.canonical, staleTime: Infinity });

  if (reference.error) return <ErrorState error={reference.error} retry={() => void reference.refetch()} />;
  if (!reference.data) return <Loading />;

  const objects = Object.entries(reference.data.objects);
  const transforms = Object.entries(reference.data.transforms);

  return (
    <>
      <PageHeader
        title="Canonical model"
        meta={
          <span>
            Every integration maps its raw records onto these objects. The model grows by addition only: renaming
            a field would break every mapping in the registry.
          </span>
        }
      />
      <div className="space-y-10">
        <IntegrationCard
          visual={<Integration />}
          title="Every source, one shape"
          description="Each integration maps raw records onto these objects, so a consumer reads one shape whether the source is BambooHR, Gusto or an internal API nobody documented."
          url="/integrations/new"
          cta="Add integration"
        />
        {objects.map(([name, schema]) => (
          <Section
            key={name}
            title={name}
            description={
              name === "Employee"
                ? `A record must map source_id and at least one identity field: ${identityFields.join(", ")}.`
                : "A record must map source_id."
            }
          >
            <Table>
              <THead>
                <TR>
                  <TH>Field</TH>
                  <TH>Type</TH>
                  <TH>Required</TH>
                  <TH>Description</TH>
                </TR>
              </THead>
              <TBody>
                {schemaFields(schema).map((field) => (
                  <TR key={field.name}>
                    <TD>
                      <Mono>{field.name}</Mono>
                    </TD>
                    <TD className="text-ink-2">{field.type}</TD>
                    <TD>{field.required ? "Yes" : "No"}</TD>
                    <TD className="text-ink-2">
                      {field.description}
                      {field.defaultValue !== undefined && field.defaultValue !== null ? (
                        <span className="text-ink-3">
                          {field.description ? " " : ""}Defaults to {JSON.stringify(field.defaultValue)}.
                        </span>
                      ) : null}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </Section>
        ))}

        <Section
          title="Transforms"
          description="The closed set of functions a mapping may use. The agent chooses from this list; no other code enters a manifest."
        >
          <Table>
            <THead>
              <TR>
                <TH>Transform</TH>
                <TH>What it does</TH>
              </TR>
            </THead>
            <TBody>
              {transforms.map(([name, doc]) => (
                <TR key={name}>
                  <TD>
                    <Mono>{name}</Mono>
                  </TD>
                  <TD className="text-ink-2">{doc || "No description"}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Section>

        <Section title="Manifest schema" description="The JSON Schema every manifest is validated against.">
          <a className="text-sm underline underline-offset-4 hover:text-ink-2" href={`${apiBase}/manifest-schema`} target="_blank" rel="noreferrer">
            Open the manifest JSON Schema
          </a>
        </Section>
      </div>
    </>
  );
}
