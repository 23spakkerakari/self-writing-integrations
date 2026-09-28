# Self-writing integrations

An agentic platform that synthesizes, verifies, publishes and maintains API integrations for the
people-management domain. Integrations are declarative manifests interpreted by a generic runtime;
an agent writes and repairs the manifests, and humans approve what goes live.

The architecture, decisions, and milestone plan live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

- **Milestone 1, built.** Manifest schema, runtime gateway with response validation, spec-derived mock
  server, verification harness, versioned registry, Claude synthesis agent, HTTP API and CLI. BambooHR
  employees mapped onto the canonical Employee object.
- **Milestone 2, built.** Multi-tenant OAuth broker: envelope-encrypted vault, PKCE consent with a
  human-approved scope screen, autonomous refresh and re-consent detection, append-only audit log,
  notifications, and a mock authorization server for offline tests. Gusto is the practice integration.
- Next: milestone 3, drift detection and the repair pipeline.

```
cd backend
pip install -e ".[dev]"
python -m pytest

python -m app.cli import manifests/bamboohr.yaml
python -m app.cli verify bamboohr 0.1.0
python -m app.cli publish bamboohr 0.1.0
python -m app.cli call bamboohr list_employees --config company_domain=acme --secret api_key=BAMBOOHR_API_KEY --mock

uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
```

Environment variables: `DATABASE_URL` (default SQLite in the working directory), `GATEWAY_MODE`
(`live` or `mock`), `SYNTHESIS_MODEL` (default `claude-opus-5`), `ANTHROPIC_API_KEY` for synthesis,
`VAULT_MASTER_KEY` (generate with `python -m app.cli vault-key`; a fixed dev key is used when unset),
`PUBLIC_BASE_URL` for the OAuth callback, `REFRESH_SCHEDULER=1` to run token refresh in-process.
