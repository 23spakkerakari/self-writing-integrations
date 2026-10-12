import { Field, Input, Label, Select } from "@/components/ui/field";

export interface FieldSpec {
  name: string;
  required?: boolean;
  description?: string;
  placeholder?: string;
}

/** One text input per known key: endpoint parameters, connection config variables. */
export function KeyValueFields({
  specs,
  values,
  onChange,
  idPrefix,
}: {
  specs: FieldSpec[];
  values: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  idPrefix: string;
}) {
  if (!specs.length) return null;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {specs.map((spec) => {
        const id = `${idPrefix}-${spec.name}`;
        return (
          <Field
            key={spec.name}
            htmlFor={id}
            hint={spec.description}
            label={
              <>
                <span className="font-mono">{spec.name}</span>
                {spec.required ? <span className="font-normal text-ink-3"> required</span> : null}
              </>
            }
          >
            <Input
              id={id}
              autoComplete="off"
              spellCheck={false}
              className="font-mono text-[13px]"
              value={values[spec.name] ?? ""}
              placeholder={spec.placeholder}
              onChange={(event) => onChange({ ...values, [spec.name]: event.target.value })}
            />
          </Field>
        );
      })}
    </div>
  );
}

export type SecretSource = "value" | "env";

/** A secret can be typed in (development) or named as an environment variable on the API host. */
export function SecretFields({
  refs,
  values,
  sources,
  onChange,
  idPrefix,
}: {
  refs: string[];
  values: Record<string, string>;
  sources: Record<string, SecretSource>;
  onChange: (values: Record<string, string>, sources: Record<string, SecretSource>) => void;
  idPrefix: string;
}) {
  if (!refs.length) return null;
  return (
    <div className="space-y-3">
      {refs.map((ref) => {
        const id = `${idPrefix}-${ref}`;
        const source = sources[ref] ?? "value";
        return (
          <div key={ref} className="space-y-1.5">
            <Label htmlFor={id}>
              <span className="font-mono">{ref}</span>
            </Label>
            <div className="flex gap-2">
              <Select
                aria-label={`How to supply ${ref}`}
                className="w-44 shrink-0"
                value={source}
                onChange={(event) => onChange(values, { ...sources, [ref]: event.target.value as SecretSource })}
              >
                <option value="value">Value</option>
                <option value="env">Environment variable</option>
              </Select>
              <Input
                id={id}
                type={source === "value" ? "password" : "text"}
                autoComplete="off"
                spellCheck={false}
                className="font-mono text-[13px]"
                placeholder={source === "value" ? "" : "BAMBOOHR_API_KEY"}
                value={values[ref] ?? ""}
                onChange={(event) => onChange({ ...values, [ref]: event.target.value }, sources)}
              />
            </div>
          </div>
        );
      })}
      <p className="text-xs text-ink-3">
        Name an environment variable on the API host where you can. Values typed here travel in the request body
        and are meant for development only.
      </p>
    </div>
  );
}

export function splitSecrets(
  refs: string[],
  values: Record<string, string>,
  sources: Record<string, SecretSource>,
): { secrets: Record<string, string>; secret_env: Record<string, string> } {
  const secrets: Record<string, string> = {};
  const secret_env: Record<string, string> = {};
  for (const ref of refs) {
    const value = values[ref]?.trim();
    if (!value) continue;
    if ((sources[ref] ?? "value") === "env") secret_env[ref] = value;
    else secrets[ref] = value;
  }
  return { secrets, secret_env };
}
