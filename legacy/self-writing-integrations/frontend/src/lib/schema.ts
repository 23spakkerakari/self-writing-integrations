import type { JsonSchema } from "./types";

/** Human-readable type for one JSON Schema node, e.g. "string, nullable" or "date". */
export function schemaType(schema: JsonSchema | undefined): string {
  if (!schema) return "any";
  const ref = schema.$ref;
  if (typeof ref === "string") return ref.split("/").pop() ?? "object";
  if (schema.enum) return schema.enum.map((value) => JSON.stringify(value)).join(" | ");
  if (schema.anyOf) {
    const members = schema.anyOf.filter((member) => member.type !== "null");
    const nullable = members.length !== schema.anyOf.length;
    const inner = members.map(schemaType).join(" | ") || "null";
    return nullable ? `${inner}, nullable` : inner;
  }
  if (Array.isArray(schema.type)) {
    const types = schema.type.filter((t) => t !== "null");
    const nullable = types.length !== schema.type.length;
    return `${types.join(" | ")}${nullable ? ", nullable" : ""}`;
  }
  if (schema.type === "array") return `${schemaType(schema.items)}[]`;
  if (schema.format) return schema.format;
  return schema.type ?? "any";
}

export interface SchemaField {
  name: string;
  type: string;
  required: boolean;
  description: string;
  defaultValue: unknown;
}

export function schemaFields(schema: JsonSchema): SchemaField[] {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).map(([name, property]) => ({
    name,
    type: schemaType(property),
    required: required.has(name),
    description: property.description ?? "",
    defaultValue: property.default,
  }));
}
