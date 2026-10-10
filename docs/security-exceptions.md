# Security scanner exceptions

Spec Section 14.8: CI fails on high or critical findings from pip-audit, osv-scanner, Trivy,
Semgrep and gitleaks unless an exception with an expiry is recorded here. An exception needs an
owner, the finding, why it does not apply or cannot be fixed yet, the compensating control, and a
date after which CI fails again.

| Opened | Expires | Scanner | Finding | Reason | Compensating control | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-10 | 2027-04-10 | pip-audit | `en-core-web-sm` 3.8.0 cannot be audited: it is not on PyPI (`--strict` fails on unauditable packages) | The spaCy English model is installed from its GitHub release URL, pinned by version and sha256 in `uv.lock` (ADR 0020); it is model data plus a thin loader package, exported with `--no-emit-package en-core-web-sm` | Hash-pinned in the lockfile; osv-scanner and Trivy scan the lockfile and the images; reviewed at every model upgrade | founder |
