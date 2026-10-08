# ADR 0020: Presidio with an explicit recognizer registry, a pinned spaCy model and sampled input

Status: accepted, 2026-10-08 (spec 6, 8.3 rule 2, 2.3 invariant 4, 17)

## Context

Presidio's default registry includes a URL recognizer that imports `tldextract`, which downloads
the public suffix list from the internet on first use and caches it; the edge must make no
outbound connection the customer did not configure (spec 2.3 invariant 4). Presidio also needs
a spaCy model, which is normally installed with `spacy download` at runtime, outside the
lockfile. And running NER on every value of every field would consume most of the throughput
budget (spec 17: 2,000 events/s).

## Decision

- `carto_edge.pipeline.pii.PresidioDetector` builds its own `RecognizerRegistry` with the
  recognizers the classifier needs (e-mail, phone, US SSN, credit card, IBAN, passport, driver
  licence, medical licence, spaCy PERSON and LOCATION). The URL recognizer is not registered; a
  test asserts it. No network import path exists in the detector.
- The spaCy model `en_core_web_sm` is a locked dependency pinned by URL and hash in `uv.lock`
  (`[tool.uv.sources]`), so images are reproducible and nothing is fetched at runtime. A larger
  model can be configured per install (`pii.spacy_model`).
- Presidio runs on a bounded sample of values per field (up to 32 at classification time, more
  on re-classification) and on template constants, never on every value. Inputs are truncated
  to 4 KB.
- When the model cannot load, or `pii.enabled` is false, a regex detector (e-mail, phone, SSN,
  credit card with Luhn, IBAN) is used and a warning is logged; name-hint rules still apply.

## Alternatives

- Presidio defaults: network fetch, slower, unpinned model.
- No Presidio: the spec names it; name hints alone miss PII in unnamed fields and in template
  constants.

## Consequences

Name recognition quality is that of the small model; the classifier's name hints carry most of
the load for structured data. The leak test is the arbiter.
