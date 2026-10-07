# carto v1 build targets (spec Section 20). Works with GNU make on Linux, macOS and Git Bash on
# Windows. Recipes use ">" as the prefix instead of a tab so the file survives any editor.
#
#   make UV="py -3.11 -m uv" test      # when uv is not on PATH (ADR 0007)

.RECIPEPREFIX := >
.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
SCENARIO ?= shop
DAYS ?= 14
SEED ?= 1
SIM_OUT ?= sim-out
EVAL_OUT ?= eval/reports

.PHONY: help setup lint fmt type test cov check schema schema-check sim eval sec sbom dev release clean

help: ## Show targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-13s %s\n", $$1, $$2}'

setup: ## Install the locked workspace (uv sync)
> $(UV) sync --locked --all-packages

lint: ## ruff lint and format check
> $(UV) run ruff check .
> $(UV) run ruff format --check .

fmt: ## Apply ruff formatting and safe fixes
> $(UV) run ruff check --fix .
> $(UV) run ruff format .

type: ## mypy --strict over every member
> $(UV) run mypy

test: ## Unit and property tests
> $(UV) run pytest

cov: ## Tests with coverage (terminal and XML)
> $(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

check: lint type test schema-check ## Everything CI's python job runs, locally

schema: ## Regenerate the committed JSON Schemas from the Pydantic models
> $(UV) run carto-schema export --out packages/carto-schema/schemas

schema-check: ## Fail if the committed JSON Schemas drift from the models
> $(UV) run carto-schema check --dir packages/carto-schema/schemas

sim: ## Generate simulator data: make sim SCENARIO=shop DAYS=14 SEED=1
> $(UV) run carto-sim generate --scenario $(SCENARIO) --days $(DAYS) --seed $(SEED) --out $(SIM_OUT)/$(SCENARIO)

eval: ## Score engine output (or the empty prediction) against ground truth: make eval SCENARIO=shop
> $(UV) run carto-eval run --scenario $(SCENARIO) --days $(DAYS) --seed $(SEED) --sim-out $(SIM_OUT)/$(SCENARIO) --out $(EVAL_OUT)

sec: ## Security scanners available locally; CI runs the full set (ADR 0007)
> $(UV) run bandit -c pyproject.toml -r packages edge core simulator eval tools -q
> $(UV) export --all-packages --no-emit-workspace --no-hashes --format requirements-txt -o requirements-export.txt
> $(UV) run pip-audit -r requirements-export.txt --strict --desc on
> @command -v semgrep >/dev/null 2>&1 && semgrep --test --metrics=off tools/semgrep && semgrep scan --config tools/semgrep --error --metrics=off edge/carto_edge/connectors && semgrep scan --config p/python --config p/secrets --error --metrics=off --exclude legacy --exclude tools/semgrep . || echo "semgrep: not installed locally, runs in CI"
> @command -v gitleaks >/dev/null 2>&1 && gitleaks git --no-banner --redact . || echo "gitleaks: not installed locally, runs in CI"
> @command -v trivy >/dev/null 2>&1 && trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --exit-code 1 --skip-dirs legacy . || echo "trivy: not installed locally, runs in CI"
> @command -v osv-scanner >/dev/null 2>&1 && osv-scanner scan source --lockfile uv.lock || echo "osv-scanner: not installed locally, runs in CI"

sbom: ## CycloneDX SBOM of the workspace (syft)
> @command -v syft >/dev/null 2>&1 && syft scan dir:. --exclude ./legacy -o cyclonedx-json=sbom.cdx.json || echo "syft: not installed locally, runs in CI"

dev: ## Compose stack + simulator live mode (arrives in M1)
> @echo "make dev: the Compose stack and simulator live mode arrive in M1 (see docs/plans/M1.md when it exists)."

release: ## Build, sign and publish images (arrives with the first service image in M1)
> @echo "make release: image build, SBOM and cosign signing of images wire up in M1; release.yml already signs the SBOM on main."

clean: ## Remove generated data and caches
> rm -rf $(SIM_OUT) $(EVAL_OUT) .pytest_cache .mypy_cache .ruff_cache .hypothesis coverage.xml .coverage requirements-export.txt sbom.cdx.json
