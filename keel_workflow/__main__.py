"""Keel's local review, verification and delivery-simulation interface."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

from keel_flow.common import keys, strict_json, timestamp, require
from .delivery import build_bundle, DeliveryStore, SyntheticAdapter
from .integration import evaluate_candidate, simulate_candidate
from .reviews import ReviewStore
from .verification import aggregate_run, compare_runs
from .recovery import evaluate_recovery


def read_json(path):
    with Path(path).open("rb") as stream:
        return strict_json(stream.read(8 * 1024 * 1024 + 1))


def load_candidate(path):
    """Attachment paths stay within the input folder; bytes are copied once."""
    path = Path(path).absolute()
    data = read_json(path)
    keys(data, {"bundle", "flow_export", "assurance_export", "trust_export"})
    config = data["bundle"]
    keys(config, {"workspace_id", "role_id", "action", "destination", "account_id", "revisions", "content", "attachments"})
    require(type(config["attachments"]) is dict, "attachment path map required")
    attachments = {}
    for name, relative in config["attachments"].items():
        require(type(relative) is str and not Path(relative).is_absolute(), "relative attachment path required")
        candidate = path.parent / relative
        require(candidate.resolve().is_relative_to(path.parent.resolve()), "attachment outside input directory")
        attachments[name] = candidate
    bundle = build_bundle(**{k: v for k, v in config.items() if k != "attachments"}, attachments=attachments)
    return bundle, {k: data[k] for k in ("flow_export", "assurance_export", "trust_export")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="run synthetic workflow and restore rehearsal in a NEW directory")
    demo.add_argument("--out", required=True)
    for name in ("candidate", "verify", "compare", "recovery"):
        item = sub.add_parser(name)
        item.add_argument("input")
        item.add_argument("--now")
        item.add_argument("--out")
        if name == "compare":
            item.add_argument("--allow-build-change", action="store_true")
    create = sub.add_parser("review-create")
    create.add_argument("input")
    create.add_argument("--expires", required=True)
    commands = [create]
    for name in ("review-task", "review-commit", "review-seal", "review-audit", "review-status", "review-invalidate"):
        item = sub.add_parser(name)
        commands.append(item)
        if name in {"review-task", "review-commit", "review-audit"}:
            item.add_argument("--reviewer", required=True)
        if name in {"review-commit", "review-audit"}:
            item.add_argument("input", help="explicit reviewer judgment JSON")
        if name == "review-task":
            item.add_argument("--phase", choices=["A", "B"], required=True)
        if name == "review-invalidate":
            item.add_argument("--reason", required=True)
        else:
            item.add_argument("--subject-sha256", required=True)
    for item in commands:
        item.add_argument("--db", required=True)
        item.add_argument("--round-id", required=True)
        item.add_argument("--now")
        item.add_argument("--out")
    approval = sub.add_parser("simulation-approve", help="record a local simulation approval only")
    approval.add_argument("input")
    approval.add_argument("--db", required=True)
    approval.add_argument("--approver", required=True)
    approval.add_argument("--authority-ref", required=True)
    approval.add_argument("--expires", required=True)
    approval.add_argument("--now")
    approval.add_argument("--out")
    simulation = sub.add_parser("simulate", help="built-in local simulator; no external adapter")
    simulation.add_argument("input")
    for option in ("review-db", "delivery-db", "round-id", "approval-id", "idempotency-key", "account-id", "authority-ref"):
        simulation.add_argument("--" + option, required=True)
    simulation.add_argument("--mode", choices=["confirmed", "timeout", "not_sent"], default="confirmed")
    simulation.add_argument("--now")
    simulation.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        now = timestamp(args.now) if getattr(args, "now", None) else datetime.now(timezone.utc)
        command = args.command
        if command == "demo":
            from .demo import run_demo
            result = run_demo(Path(args.out))
        elif command == "candidate":
            bundle, context = load_candidate(args.input)
            result = evaluate_candidate(bundle, **context, now=now)
        elif command in {"verify", "compare", "recovery"}:
            data = read_json(args.input)
            if command == "verify":
                keys(data, {"spec", "observations"})
                result = aggregate_run(data["spec"], data["observations"])
            elif command == "compare":
                keys(data, {"previous", "current"})
                result = compare_runs(data["previous"], data["current"], allow_build_change=args.allow_build_change)
            else:
                keys(data, {"expected_manifest", "observed_inventory", "verification_run"})
                result = evaluate_recovery(**data)
        elif command.startswith("review-"):
            store = ReviewStore(args.db)
            rid = args.round_id
            if command == "review-create":
                bundle, context = load_candidate(args.input)
                candidate = evaluate_candidate(bundle, **context, now=now)
                require(candidate["state"] == "READY_FOR_BLIND_REVIEW", "candidate has unresolved gates")
                result = store.create_round(rid, candidate["subject"], candidate["reviewer_ids"], timestamp(args.expires), now=now)
            elif command == "review-invalidate":
                result = store.invalidate(rid, args.reason, now=now)
            elif command == "review-task":
                method = store.phase_a if args.phase == "A" else store.phase_b
                result = method(rid, args.reviewer, args.subject_sha256, now=now)
            elif command in {"review-commit", "review-audit"}:
                data = read_json(args.input)
                fields = {"verdict", "findings"} | ({"covered_claim_ids"} if command == "review-commit" else set())
                keys(data, fields)
                method = store.commit if command == "review-commit" else store.audit
                result = method(rid, args.reviewer, args.subject_sha256, **data, now=now)
            elif command == "review-seal":
                result = store.seal(rid, args.subject_sha256, now=now)
            else:
                result = store.evaluate(rid, args.subject_sha256, now=now)
        else:
            bundle, context = load_candidate(args.input)
            delivery = DeliveryStore(args.db if command == "simulation-approve" else args.delivery_db)
            try:
                if command == "simulation-approve":
                    candidate = evaluate_candidate(bundle, **context, now=now)
                    require(candidate["state"] == "READY_FOR_BLIND_REVIEW", "candidate has unresolved gates")
                    result = delivery.approve(bundle, approver_id=args.approver, authority_ref=args.authority_ref,
                                              expires_at=timestamp(args.expires), now=now)
                else:
                    result = simulate_candidate(bundle, **context, reviews=ReviewStore(args.review_db),
                                                round_id=args.round_id, delivery=delivery, approval_id=args.approval_id,
                                                idempotency_key=args.idempotency_key, adapter=SyntheticAdapter(mode=args.mode),
                                                current_account_id=args.account_id, current_authority_ref=args.authority_ref, now=now)
            finally:
                delivery.close()
        rendered = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if getattr(args, "out", None) and command != "demo":
            descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
        blocked = result.get("state") in {"BLOCKED", "HOLD", "WAITING_FOR_PHASE_A", "WAITING_FOR_SEAL"}
        failed = result.get("status") in {"FAIL", "INCOMPLETE", "NOT_READY"}
        if command == "simulate" and result["simulation"]["status"] != "SIMULATED_CONFIRMED":
            failed = True
        return 3 if blocked or failed else 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError) as exc:
        print("keel-workflow: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
