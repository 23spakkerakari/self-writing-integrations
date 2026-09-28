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
- **Milestone 3, built.** Drift detection and repair: the gateway's drift events (schema, status, auth,
  deprecation, pagination, mapping) aggregate into incidents; deterministic triage assigns class and risk;
  a mechanical patch or the repair agent produces a candidate that is verified against the API as it
  behaves now; change requests wait for approval (or a per-risk policy), run a canary, promote, and roll
  back in one step. Drift worlds simulate every drift type against the mock.
- **Developer console, built.** A React/Vite app under `frontend/`: integrations, versions and their
  verification reports, a call console, tenant connections with the consent screen, drift incidents and
  change requests behind the approval gate, per-integration approval policies, rollback, the activity
  feed and the canonical model reference. Runs against the API in mock or live mode.
- Next: milestone 4, the traffic-first ingester.

```
cd backend
pip install -e ".[dev]"
python -m pytest

python -m app.cli import manifests/bamboohr.yaml
python -m app.cli verify bamboohr 0.1.0
python -m app.cli publish bamboohr 0.1.0
python -m app.cli call bamboohr list_employees --config company_domain=acme --secret api_key=BAMBOOHR_API_KEY --mock

uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs

cd ../frontend
npm install
npm run dev                        # http://127.0.0.1:5173, proxies /api to the backend
```

Environment variables: `DATABASE_URL` (default SQLite in the working directory), `GATEWAY_MODE`
(`live` or `mock`), `SYNTHESIS_MODEL` (default `claude-opus-5`), `ANTHROPIC_API_KEY` for synthesis,
`VAULT_MASTER_KEY` (generate with `python -m app.cli vault-key`; a fixed dev key is used when unset),
`PUBLIC_BASE_URL` for the OAuth callback, `REFRESH_SCHEDULER=1` to run token refresh in-process,
`DRIFT_WORKER=1` to run the drift worker in-process (`DRIFT_INTERVAL_SECONDS`, `CANARY_FRACTION`,
`CANARY_MIN_CALLS`, `REPAIR_MAX_ROUNDS` tune it).
