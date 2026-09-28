"""Command-line front end.

    python -m app.cli import manifests/bamboohr.yaml
    python -m app.cli verify bamboohr 0.1.0
    python -m app.cli publish bamboohr 0.1.0
    python -m app.cli call bamboohr list_employees --config company_domain=acme --secret api_key=BAMBOOHR_API_KEY --mock
    python -m app.cli synthesize specs/bamboohr-employees.openapi.yaml --name bamboohr
    python -m app.cli incidents [--integration bamboohr] [--status open]
    python -m app.cli changes [--status pending]
    python -m app.cli approve 3 --actor pradhi
    python -m app.cli promote 3 --actor pradhi
    python -m app.cli rollback bamboohr --actor pradhi
    python -m app.cli policy bamboohr low on
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.config import load_settings
from app.drift.changes import ChangeRequests
from app.drift.monitor import DriftMonitor
from app.manifest.schema import load_manifest, load_manifest_file
from app.registry.store import Registry
from app.runtime.gateway import Connection, Gateway
from app.runtime.secrets import EnvSecretsProvider
from app.synthesis.pipeline import synthesize_and_verify
from app.verification.harness import verify
from app.verification.mock_server import MockServer


def _kv(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        out[key] = value
    return out


def cmd_import(args: argparse.Namespace, registry: Registry) -> None:
    manifest = load_manifest_file(args.path)
    record = registry.create_version(manifest, provenance="cli-import")
    print(f"stored {record.name}@{record.version} ({record.status})")


def cmd_verify(args: argparse.Namespace, registry: Registry) -> None:
    record = registry.get_version(args.name, args.version)
    report = verify(load_manifest(record.manifest), mode="mock")
    registry.record_verification(args.name, args.version, report.model_dump(mode="json"), report.passed)
    print(report.summary())
    sys.exit(0 if report.passed else 1)


def cmd_publish(args: argparse.Namespace, registry: Registry) -> None:
    record = registry.publish(args.name, args.version)
    print(f"published {record.name}@{record.version}")


def cmd_list(args: argparse.Namespace, registry: Registry) -> None:
    for item in registry.list_integrations():
        print(f"{item.name:20} published={item.published_version or '-':8} latest={item.latest_version or '-':8} versions={item.version_count}")


def cmd_call(args: argparse.Namespace, registry: Registry) -> None:
    record = registry.get_version(args.name, args.version) if args.version else registry.get_published(args.name)
    manifest = load_manifest(record.manifest)
    transport = MockServer(manifest).transport() if args.mock else None
    connection = Connection(config=_kv(args.config))
    secrets = EnvSecretsProvider(_kv(args.secret))
    params: dict[str, Any] = _kv(args.param)
    with Gateway(manifest, connection, secrets, transport=transport) as gateway:
        result = gateway.call(args.endpoint, params, paginate=not args.no_paginate)
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    sys.exit(0 if result.ok else 1)


def cmd_synthesize(args: argparse.Namespace, registry: Registry) -> None:
    settings = load_settings()
    with open(args.path, encoding="utf-8") as fh:
        spec_text = fh.read()
    result = synthesize_and_verify(
        registry, spec_text, args.name, model=settings.synthesis_model, max_attempts=settings.synthesis_max_attempts, max_rounds=args.rounds
    )
    print(result.report.summary())
    print(f"stored {result.record.name}@{result.record.version} ({result.record.status}) after {result.rounds} round(s), {result.model_attempts} model call(s)")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result.record.manifest, fh, indent=2)
        print(f"manifest written to {args.out}")


def cmd_vault_key(args: argparse.Namespace, registry: Registry) -> None:
    from app.oauth.vault import Vault

    print(Vault.generate_master_key())


def _drift(registry: Registry) -> tuple[DriftMonitor, ChangeRequests]:
    monitor = DriftMonitor(registry.db, registry)
    return monitor, ChangeRequests(registry.db, registry, monitor)


def cmd_incidents(args: argparse.Namespace, registry: Registry) -> None:
    monitor, _ = _drift(registry)
    for i in monitor.list(integration=args.integration, status=args.status):
        print(
            f"#{i.id:<4} {i.integration:12} {i.endpoint_id:18} {i.kind:20} {i.status:14} "
            f"class={i.drift_class or '-':11} risk={i.risk_class or '-':6} count={i.count:<4} cr={i.change_request_id or '-'}"
        )
        for sample in i.samples[:3]:
            print(f"        - {sample[:110]}")


def cmd_changes(args: argparse.Namespace, registry: Registry) -> None:
    _, changes = _drift(registry)
    for c in changes.list(integration=args.integration, status=args.status):
        print(
            f"#{c.id:<4} {c.integration:12} {c.base_version} -> {c.candidate_version:8} {c.status:11} "
            f"risk={c.risk_class:6} verified={str(c.verified):5} {c.strategy}"
        )


def cmd_approve(args: argparse.Namespace, registry: Registry) -> None:
    _, changes = _drift(registry)
    change = changes.approve(args.change_id, args.actor, args.note)
    print(f"change request {change.id} {change.status} by {change.decided_by}")


def cmd_reject(args: argparse.Namespace, registry: Registry) -> None:
    _, changes = _drift(registry)
    change = changes.reject(args.change_id, args.actor, args.note)
    print(f"change request {change.id} {change.status} by {change.decided_by}")


def cmd_promote(args: argparse.Namespace, registry: Registry) -> None:
    _, changes = _drift(registry)
    change = changes.promote(args.change_id, args.actor, force=args.force)
    print(f"change request {change.id} {change.status}: {change.integration}@{change.candidate_version} is live")


def cmd_rollback(args: argparse.Namespace, registry: Registry) -> None:
    _, changes = _drift(registry)
    record = changes.rollback(args.name, args.actor, args.note)
    print(f"{record.name}@{record.version} is published again")


def cmd_policy(args: argparse.Namespace, registry: Registry) -> None:
    _, changes = _drift(registry)
    policy = changes.set_policy(args.name, args.risk_class, args.state == "on", actor=args.actor)
    print(f"{policy.integration}: auto-approve {policy.auto_approve}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="store a manifest file as a draft version")
    p.add_argument("path")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("verify", help="run mock verification on a stored version")
    p.add_argument("name")
    p.add_argument("version")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("publish", help="publish a verified version")
    p.add_argument("name")
    p.add_argument("version")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("list", help="list integrations")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("call", help="call an endpoint through the gateway")
    p.add_argument("name")
    p.add_argument("endpoint")
    p.add_argument("--version")
    p.add_argument("--param", action="append", default=[], help="name=value")
    p.add_argument("--config", action="append", default=[], help="config_var=value")
    p.add_argument("--secret", action="append", default=[], help="secret_ref=ENV_VAR_NAME")
    p.add_argument("--mock", action="store_true", help="route to the spec-derived mock instead of the real API")
    p.add_argument("--no-paginate", action="store_true")
    p.set_defaults(fn=cmd_call)

    p = sub.add_parser("synthesize", help="have the agent build a manifest from a spec, verify it, and store it")
    p.add_argument("path")
    p.add_argument("--name", required=True)
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--out", help="also write the resulting manifest JSON here")
    p.set_defaults(fn=cmd_synthesize)

    p = sub.add_parser("vault-key", help="print a new base64 VAULT_MASTER_KEY")
    p.set_defaults(fn=cmd_vault_key)

    p = sub.add_parser("incidents", help="list drift incidents")
    p.add_argument("--integration")
    p.add_argument("--status")
    p.set_defaults(fn=cmd_incidents)

    p = sub.add_parser("changes", help="list change requests")
    p.add_argument("--integration")
    p.add_argument("--status")
    p.set_defaults(fn=cmd_changes)

    for name, fn, help_text in (
        ("approve", cmd_approve, "approve a pending change request"),
        ("reject", cmd_reject, "reject a change request"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("change_id", type=int)
        p.add_argument("--actor", default="cli")
        p.add_argument("--note", default="")
        p.set_defaults(fn=fn)

    p = sub.add_parser("promote", help="publish an approved change request's candidate")
    p.add_argument("change_id", type=int)
    p.add_argument("--actor", default="cli")
    p.add_argument("--force", action="store_true", help="promote even if the canary verdict is not pass")
    p.set_defaults(fn=cmd_promote)

    p = sub.add_parser("rollback", help="re-publish the previous version in one step")
    p.add_argument("name")
    p.add_argument("--actor", default="cli")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("policy", help="allow or forbid auto-approval per risk class")
    p.add_argument("name")
    p.add_argument("risk_class", choices=["low", "medium", "high"])
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--actor", default="cli")
    p.set_defaults(fn=cmd_policy)

    args = parser.parse_args(argv)
    registry = Registry(load_settings().database_url)
    args.fn(args, registry)


if __name__ == "__main__":
    main()
