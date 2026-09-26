"""Local source producers, explicit review decisions, and export for Keel."""
import argparse
from datetime import datetime, timezone
import json
import sqlite3
import sys

from keel_agent.io import read_json, write_private
from keel_flow.common import timestamp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="read-only host capabilities; no installs or inference")
    doctor.add_argument("--config")
    doctor.add_argument("--detailed", action="store_true")
    doctor.add_argument("--out")
    commands = {}
    for name in ("init", "scope-add", "import-scopes", "capture", "capture-attachments", "export", "status",
                 "approval-request", "approval-show", "approval-decide", "approval-revoke"):
        item = sub.add_parser(name)
        commands[name] = item
        item.add_argument("--home", required=True, help="private local source-store directory")
        item.add_argument("--workspace", required=True)
        item.add_argument("--out", help="new private JSON output file; existing files refused")
    for name in ("scope-add", "import-scopes", "capture", "capture-attachments"):
        commands[name].add_argument("input")
    commands["import-scopes"].add_argument("--action", required=True)
    for name in ("capture", "capture-attachments", "approval-request"):
        commands[name].add_argument("--scope", required=True, help="JSON with exact workspace/role/application/action")
    for name in ("capture", "capture-attachments"):
        commands[name].add_argument("--expected-generation", required=True, type=int)
    commands["capture"].add_argument("--component", choices=["policy", "form", "answers", "attachments", "target", "route"], required=True)
    commands["capture-attachments"].add_argument("--source-root", required=True)
    for name in ("export", "status"):
        commands[name].add_argument("--flow", help="actual current canonical flow JSON to join by exact identity")
        commands[name].add_argument("--scope", help="optional single-scope JSON")
    commands["approval-request"].add_argument("--expires-at", required=True)
    for name in ("approval-show", "approval-decide", "approval-revoke"):
        commands[name].add_argument("--request-id", required=True)
    for name in ("approval-decide", "approval-revoke"):
        commands[name].add_argument("--actor", required=True, help="trusted host's actual operator identity assertion")
    decision = commands["approval-decide"]
    decision.add_argument("--decision", choices=["APPROVE", "REJECT"], required=True)
    decision.add_argument("--authority-ref", required=True)
    decision.add_argument("--reviewed-sha256", required=True)
    decision.add_argument("--expires-at", required=True)
    commands["approval-revoke"].add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            from .host import inspect_host
            result = inspect_host(read_json(args.config) if args.config else None, detailed=args.detailed)
        else:
            from .store import SourceStore
            store = SourceStore(args.home, args.workspace)
            if args.command == "init":
                result = {"state": "INITIALIZED", "workspace_id": args.workspace, "source_records_created": 0,
                          "execution_authorized": False}
            elif args.command == "scope-add":
                result = store.register_scope(read_json(args.input))
            elif args.command == "import-scopes":
                from .service import register_flow_scopes
                result = register_flow_scopes(store, read_json(args.input), action=args.action)
            elif args.command in {"capture", "capture-attachments"}:
                scope, descriptor = read_json(args.scope), read_json(args.input)
                component = args.component if args.command == "capture" else "attachments"
                if args.command == "capture-attachments":
                    from .capture import ingest_attachments
                    descriptor = ingest_attachments(descriptor, source_root=args.source_root,
                        attachment_root=store.attachment_root, scope=scope, now=store.clock())
                result = store.put_source(scope, component, descriptor, expected_generation=args.expected_generation)
            elif args.command in {"export", "status"}:
                from .service import build_export, status_summary
                result = build_export(store, scopes=[read_json(args.scope)] if args.scope else None,
                                      flow=read_json(args.flow) if args.flow else None)
                if args.command == "status":
                    result = status_summary(result)
            else:
                from .decisions import prepare_request, get_request, decide_request, revoke_request
                if args.command == "approval-request":
                    result = prepare_request(store, read_json(args.scope), expires_at=timestamp(args.expires_at).isoformat())
                elif args.command == "approval-show":
                    result = get_request(store, args.request_id)
                elif args.command == "approval-decide":
                    result = decide_request(store, args.request_id, decision=args.decision, actor_id=args.actor,
                        authority_record_ref=args.authority_ref, reviewed_sha256=args.reviewed_sha256,
                        expires_at=timestamp(args.expires_at).isoformat())
                else:
                    result = revoke_request(store, args.request_id, actor_id=args.actor, reason=args.reason)
        if isinstance(result, dict):
            result["execution_authorized"] = False
        if args.out:
            write_private(args.out, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        if args.command in {"export", "status"}:
            return 0 if result["scope_count"] > 0 and result["producer_inputs_complete_count"] == result["scope_count"] else 3
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError) as exc:
        # Source/model/browser text never becomes executable code. Input errors
        # are validation diagnostics; consumers must check the exit status.
        print("keel-sources: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
