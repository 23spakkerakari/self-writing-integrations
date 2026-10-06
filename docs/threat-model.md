# Threat model

Maintained per spec Section 15. Reviewed at the end of every milestone; the review log is at the
bottom. Each row names the mitigation and the automated check that proves it.

| # | Threat | Mitigations | Verified by | Status |
| --- | --- | --- | --- | --- |
| 1 | Stolen connector credentials used to read customer systems | Secret manager references, least privilege, read-only verification, rotation, egress policies | Secrets never present in DB, API responses or logs (tests) | M1 |
| 2 | Core database exfiltration | Core holds tokens, shapes and low-cardinality attributes only | Leak test (Section 18.3) | M1 |
| 3 | Dictionary attack on tokens through search | Rate limits, audit, self-alert on unusual search volume per user, key never leaves edge | Rate-limit and audit tests | M3 |
| 4 | Malicious log content causing XSS | Text-only rendering, strict CSP | Playwright XSS corpus | M2 |
| 5 | Prompt injection through log content | Data fencing, no tools, schema-validated output, human confirmation | Injection corpus yields only schema-valid suggestions | M2 |
| 6 | SSRF via connector or channel URLs | DNS resolution checks, IP pinning, allowlists | SSRF suite (metadata IPs, rebinding, redirects) | M1 |
| 7 | ReDoS, XXE, zip bombs | RE2, defusedxml, archive limits | Payload fixtures | M1 |
| 8 | Insider bulk-revealing values | Separate grant, step-up auth, rate limits, audit, alert on bulk reveal | Authz and rate tests | M3 |
| 9 | Supply chain compromise | Signed images, SBOM, pinned dependencies, scanners, CODEOWNERS | CI gates | M0 (lockfile with hashes, scanners, SBOM signing, CODEOWNERS, Renovate); images from M1 |
| 10 | Log flood or denial of service | Backpressure, per-source quotas, buffer caps, flood sampling that keeps errors | Load tests | M1 |
| 11 | Audit tampering | Hash chain, INSERT-only grants, external anchoring | `audit verify` test | M6 |
| 12 | Session hijack, CSRF | Cookie flags, CSRF tokens, short sessions, step-up | Security tests, ZAP | M6 |
| 13 | Accidental write to a source system | No write paths, read-only sessions, grant checks, Semgrep rule | Read-only tests | M0 (Semgrep rule with self-test); connectors M1 |
| 14 | PHI leaking into chat or email | `include_identifiers` off by default, UI warning | Notification content tests | M4 |
| 15 | Loss of tokenization key | KMS-managed wrapping, documented backup and recovery | Restore drill | M6 |

## Assets

Raw identifier values and PII (edge only), the tokenization key (edge memory, KMS-wrapped at
rest), the reveal vault, connector credentials (customer secret manager), the customer's
integration architecture (core), the audit log.

## Trust boundaries

Customer systems to edge (read-only, least privilege), edge to core (mTLS, tokenized data only),
core to channels and LLM endpoint (allowlisted egress, metadata only), users to API (OIDC, RBAC,
step-up for reveal).

## Review log

- **M0 (2026-10-06):** no runtime components exist yet. Established: hashed lockfile, CI
  scanners failing on high and critical findings, SBOM generation and keyless signing on `main`,
  CODEOWNERS for the sensitive paths, Renovate with pinned action digests, the Semgrep read-only
  connector rule with a self-test, and a simulator that plants marker values for the M1 leak
  test. The simulator writes synthetic PII only (names, emails and addresses are generated and
  carry markers); no real customer data exists in the repository.
