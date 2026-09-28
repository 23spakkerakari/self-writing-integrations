"""Synthesis agent: turn an API spec into an integration manifest with Claude.

The agent never writes code. It emits a manifest that must validate against the schema in
app.manifest.schema, including the cross-checks (known canonical fields, known transforms,
declared path params). Validation failures and verification failures are fed back as the
next turn so the model can correct its own output.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from app.canonical.people import canonical_schemas
from app.manifest.schema import IntegrationManifest, load_manifest
from app.runtime.transforms import TRANSFORM_DOCS


class SynthesisError(Exception):
    pass


class SynthesisResult(BaseModel):
    manifest: IntegrationManifest
    attempts: int
    transcript: list[str]


def _system_prompt() -> str:
    manifest_schema = json.dumps(IntegrationManifest.model_json_schema(), indent=1)
    canonical = json.dumps(canonical_schemas(), indent=1)
    transforms = "\n".join(f"- {name}: {doc}" for name, doc in TRANSFORM_DOCS.items())
    return f"""You build integration manifests for a people-management data platform.

A manifest is a declarative description of a third-party HTTP API that a generic runtime
executes. You are given an API specification (OpenAPI, docs text, or observed traffic) and
must produce one manifest as JSON that validates against this JSON Schema:

{manifest_schema}

Canonical objects the runtime maps records onto (mapping targets must be their field names,
excluding source_integration):

{canonical}

Transforms available in FieldMap.transform (no other names are accepted):
{transforms}

Rules:
- Include every read endpoint in the spec that yields Employee or Department data. Skip endpoints that mutate data.
- Path variables in an endpoint path must be declared as path parameters.
- base_url may contain {{variables}} for tenant-specific pieces (subdomain, company id); list each one in config_vars.
- Use the spec's auth scheme exactly. Reference secrets by a short secret_ref like "api_key"; never invent credential values.
- Give each endpoint a response_schema (JSON Schema draft 2020-12) that describes the real response body, including
  nullable fields as {{"type": ["string", "null"]}}. Be strict about required fields you are sure of, lenient otherwise.
- Set items_path to the dotted path of the record list inside the body, or omit it when the body is one record.
- Configure pagination only when the spec documents it.
- Add a mapping for every endpoint that yields canonical data. Source paths are relative to one record.
  Map source_id always. Map every canonical field the record can populate; use enum_map for status fields
  and to_date for dates. Do not map fields the record cannot supply.
- Choose a lowercase slug for the manifest name and stable slugs for endpoint ids.
- Output only the manifest JSON."""


def _request(client: anthropic.Anthropic, model: str, system: str, messages: list[dict[str, Any]], structured: bool) -> str:
    kwargs: dict[str, Any] = dict(model=model, max_tokens=16000, system=system, messages=messages)
    if structured:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": IntegrationManifest.model_json_schema()}
        }
    response = client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        raise SynthesisError("the model declined the request")
    if response.stop_reason == "max_tokens":
        raise SynthesisError("the model ran out of output tokens before finishing the manifest")
    text = "".join(block.text for block in response.content if block.type == "text")
    return text


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object found in the response")
    return json.loads(text[start : end + 1])


def synthesize(
    spec_text: str,
    name_hint: str,
    model: str = "claude-opus-5",
    max_attempts: int = 3,
    client: anthropic.Anthropic | None = None,
    previous: IntegrationManifest | None = None,
    feedback: str | None = None,
) -> SynthesisResult:
    """Produce a validated manifest. With `previous` and `feedback`, repair an existing one instead."""
    client = client or anthropic.Anthropic()
    system = _system_prompt()
    transcript: list[str] = []

    if previous is not None:
        task = (
            f"The manifest below failed verification. Fix it so the problems go away, keeping everything that works.\n\n"
            f"Problems:\n{feedback}\n\nCurrent manifest:\n{previous.model_dump_json(indent=1)}\n\nAPI specification:\n{spec_text}"
        )
    else:
        task = f"Integration name hint: {name_hint}\n\nAPI specification:\n{spec_text}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]

    structured = True
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            text = _request(client, model, system, messages, structured)
        except anthropic.BadRequestError as exc:
            if structured:
                # The manifest schema uses features the structured-output validator may not accept; fall back to free text.
                structured = False
                text = _request(client, model, system, messages, structured)
            else:
                raise SynthesisError(f"API rejected the request: {exc}") from exc
        transcript.append(text)
        try:
            manifest = load_manifest(_extract_json(text))
            return SynthesisResult(manifest=manifest, attempts=attempt, transcript=transcript)
        except (ValueError, ValidationError) as exc:
            last_error = _format_error(exc)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"That manifest is invalid:\n{last_error}\n\nReturn a corrected manifest JSON."})
    raise SynthesisError(f"no valid manifest after {max_attempts} attempts; last error: {last_error}")


def _format_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "\n".join(f"- {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:25])
    return str(exc)
