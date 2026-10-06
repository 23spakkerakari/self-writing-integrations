"""Discovery and mapping: learn what an undocumented API looks like from its traffic.

Captured exchanges (a HAR export, the recording transport, safe probes) are redacted at ingest and
stored as samples. Samples are clustered into endpoints, response bodies are merged into schemas
with per-field statistics, and every field is matched against the canonical model. A match is only
a proposal: a person confirms or rejects it, and only confirmed proposals reach a manifest draft.
"""
