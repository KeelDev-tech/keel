"""Keel's self-hosted source revision, state and local review interface."""
import argparse
import json
import sqlite3
import sys

from .io import read_json, write_private
from .runtime import LocalAgent, load_bundle, public_job, utcnow


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    revisions = sub.add_parser("revisions", help="derive seven revisions from actual normalized source records")
    revisions.add_argument("input")
    revisions.add_argument("--attachments", required=True)
    revisions.add_argument("--out")
    commands = {}
    for name in ("init", "snapshot-import", "snapshot-seal", "enqueue-review", "worker-once", "preflight",
                 "approve-prepare", "prepare-browser", "job", "events"):
        item = sub.add_parser(name)
        commands[name] = item
        item.add_argument("--home", required=True)
        item.add_argument("--workspace", required=True)
        item.add_argument("--out")
    for name in ("snapshot-import", "snapshot-seal", "enqueue-review", "prepare-browser"):
        commands[name].add_argument("input")
    seal = commands["snapshot-seal"]
    for option in ("source-revision", "captured-at", "expires-at"):
        seal.add_argument("--"+option, required=True)
    seal.add_argument("--sequence", required=True, type=int)
    review = commands["enqueue-review"]
    for option in ("config", "idempotency-key", "round-id"):
        review.add_argument("--"+option, required=True)
    review.add_argument("--ttl", type=int, default=600)
    commands["worker-once"].add_argument("--worker", default="local-worker")
    for name in ("preflight", "approve-prepare", "prepare-browser", "job"):
        commands[name].add_argument("--job-id", required=True)
    commands["approve-prepare"].add_argument("--expires-at", required=True)
    commands["prepare-browser"].add_argument("--approval-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "revisions":
            from .revisions import export_revisions
            result = export_revisions(read_json(args.input), attachment_root=args.attachments, now=utcnow())
        else:
            agent = LocalAgent(args.home, args.workspace)
            if args.command == "init":
                result = {"state": "INITIALIZED", "workspace_id": args.workspace, "execution_authorized": False}
            elif args.command == "snapshot-seal":
                result = agent.state.sign_snapshot(read_json(args.input), args.source_revision, args.sequence,
                                                   args.captured_at, args.expires_at)
            elif args.command == "snapshot-import":
                imported = agent.state.import_snapshot(read_json(args.input), now=agent.clock())
                result = {key: imported[key] for key in ("sequence", "body_sha256", "execution_authorized")}
            elif args.command == "enqueue-review":
                result = public_job(agent.enqueue_review(load_bundle(args.input), read_json(args.config),
                    idempotency_key=args.idempotency_key, round_id=args.round_id, ttl_seconds=args.ttl))
            elif args.command == "worker-once":
                result = public_job(agent.worker_once(args.worker))
            elif args.command == "job":
                result = public_job(agent.state.get_job(args.job_id))
            elif args.command == "events":
                result = {"events": agent.state.events(), "execution_authorized": False}
            elif args.command == "preflight":
                checked = agent.preflight(args.job_id)
                result = {key: checked[key] for key in ("state", "reasons", "execution_authorized")}
            elif args.command == "approve-prepare":
                result = {"approval_id": agent.approve_prepare(args.job_id, expires_at=args.expires_at),
                          "action": "PREPARE", "execution_authorized": False}
            else:
                from .browser import BrowserAdapter
                result = agent.prepare_browser(args.job_id, read_json(args.input), approval_id=args.approval_id,
                                               adapter=BrowserAdapter())
        if args.out:
            write_private(args.out, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        if result.get("state") in {"BLOCKED", "FAILED", "HOLD"} or (
                result.get("review") is not None and result["review"]["state"] != "READY_FOR_HUMAN_REVIEW"):
            return 3
        if args.command == "revisions" and (result["role_count"] == 0 or result["blocked_role_count"] > 0):
            return 3
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError) as exc:
        print("keel-agent: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
