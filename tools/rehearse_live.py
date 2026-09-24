#!/usr/bin/env python3
"""Exercise Keel's capture-to-review bridge using isolated synthetic fixtures.

PASS means these local contract checks passed. It never means a live role,
operator authentication, inference, browser preparation or submission succeeded.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keel_agent.io import write_private
from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS
from keel_live.proof import build_proof
from tools.make_live_demo import create_demo, NOW, SYNTHETIC_WORKSPACE


def _check(checks, name, condition):
    checks.append({"name": name, "passed": bool(condition)})
    if not condition:
        raise AssertionError("synthetic live rehearsal failed: " + name)


def _refuses(operation):
    try:
        operation()
    except ValueError:
        return True
    return False


def run_rehearsal(output):
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    checks = []
    demo = create_demo(output / "synthetic-complete")
    six = demo.export()["source_export"]
    _check(checks, "six_real_fixture_events_do_not_create_approval",
        six["source_record_coverage"] == {name: int(name != "approval") for name in COMPONENTS}
        and six["revision_report"]["human_root_cause_count"] == 0
        and demo.review.inspect(demo.principal, demo.scope)["current_request"] is None)
    source_times = {key: six["snapshot"]["roles"][0]["sources"][key]["observed_at"]
                    for key in PREAPPROVAL_COMPONENTS}
    _check(checks, "capture_preserves_producer_observation_times",
        source_times == {key: demo.sources[key]["observed_at"] for key in PREAPPROVAL_COMPONENTS}
        and all(value == (NOW - timedelta(seconds=20)).isoformat() for value in source_times.values()))
    manifest = six["snapshot"]["roles"][0]["sources"]["attachments"]["record"]["files"][0]
    _check(checks, "attachment_revisions_use_actual_staged_fixture_bytes",
        (demo.store.attachment_root / manifest["path"]).read_bytes()
        == (demo.source_root / "synthetic-resume.txt").read_bytes())
    before = demo.store.db_path.read_bytes()
    _check(checks, "absent_host_principal_cannot_inspect_private_material",
        _refuses(lambda: demo.review.inspect(None, demo.scope))
        and demo.store.db_path.read_bytes() == before)
    _check(checks, "six_source_proof_remains_blocked", demo.proof()["state"] == "BLOCKED")
    pending = demo.request()
    _check(checks, "only_explicit_request_creates_pending_decision",
        pending["state"] == "PENDING"
        and demo.export()["source_export"]["revision_report"]["human_root_cause_count"] == 1)
    before = demo.store.db_path.read_bytes()
    _check(checks, "absent_host_principal_cannot_decide",
        _refuses(lambda: demo.review.decide(None, demo.scope, pending["request_id"], decision="APPROVE",
            reviewed_sha256=pending["review_sha256"], expires_at=(NOW+timedelta(minutes=2)).isoformat()))
        and demo.store.db_path.read_bytes() == before)
    approved = demo.decide(pending)
    _check(checks, "explicit_fixture_decision_produces_seventh_revision",
        approved["state"] == "APPROVED" and approved["approval_currently_valid"]
        and len(demo.export()["source_export"]["revision_report"]["roles"][0]["revisions"]) == 7)
    unbound = demo.proof()
    _check(checks, "source_approval_cannot_invent_canonical_reviews_or_dependencies",
        unbound["state"] == "BLOCKED"
        and not unbound["roles"][0]["packet_dependencies_bound"]
        and not unbound["roles"][0]["assurance_checks_passed"]
        and not unbound["roles"][0]["trust_checks_passed"])
    demo.bind_synthetic_checks()
    snapshot = demo.export()["workbench_snapshot"]
    proof = demo.proof()
    _check(checks, "explicit_synthetic_host_binding_passes_existing_review_checks",
        proof["state"] == "CAPTURE_TO_REVIEW_CHECKS_PASSED"
        and proof["counts"]["synthetic_capture_review_checks_passed"] == 1)
    _check(checks, "synthetic_pass_never_counts_as_operational_or_rendered_success",
        proof["counts"]["declared_operational_capture_review_checks_passed"] == 0
        and proof["counts"]["verified_rendered_preparations"] == 0
        and all(row["status"] == "NOT_MET" for row in proof["milestones"])
        and all(value == "NOT_RUN" for value in proof["runtime_checks"].values()))
    before = demo.store.db_path.read_bytes()
    demo.review.inspect(demo.principal, demo.scope)
    demo.review.get(demo.principal, demo.scope, pending["request_id"])
    demo.export()
    demo.proof()
    _check(checks, "inspection_export_and_proof_do_not_write_source_database",
        demo.store.db_path.read_bytes() == before)
    demo.restart()
    _check(checks, "restart_preserves_reviews_sources_and_exact_bindings",
        demo.export()["workbench_snapshot"] == snapshot
        and demo.proof() == proof
        and demo.review.get(demo.principal, demo.scope, pending["request_id"])["approval_currently_valid"])
    wrong = deepcopy(demo.flow)
    wrong["leads"][0]["identity"] = "a" * 64
    _check(checks, "mismatched_current_flow_identity_is_rejected",
        _refuses(lambda: demo.export(flow=wrong)))
    stale = build_proof(snapshot, workspace_id=SYNTHETIC_WORKSPACE, synthetic=True,
        action="PREPARE", attachment_root=demo.store.attachment_root, now=NOW+timedelta(seconds=601))
    _check(checks, "stale_snapshot_never_refreshes_itself_or_qualifies",
        stale["state"] == "BLOCKED" and not stale["current"]
        and snapshot["flow"]["observed_at"] == NOW.isoformat())
    demo.changed_answer()
    changed = demo.proof()
    _check(checks, "changed_answer_invalidates_approval_and_bound_packet",
        changed["state"] == "BLOCKED"
        and changed["roles"][0]["components"]["approval"]["reason"] == "approval_dependency_mismatch"
        and not demo.review.get(demo.principal, demo.scope, pending["request_id"])["approval_currently_valid"])
    rejected = create_demo(output / "synthetic-rejected")
    rejected.decide(rejected.request(), "REJECT")
    rejection = rejected.proof()
    _check(checks, "explicit_rejection_remains_blocked",
        rejection["state"] == "BLOCKED"
        and rejection["roles"][0]["components"]["approval"]["reason"] == "approval_rejected")
    revoked = create_demo(output / "synthetic-revoked")
    revocable = revoked.decide(revoked.request())
    revoked.bind_synthetic_checks()
    revoked.review.revoke(revoked.principal, revoked.scope, revocable["request_id"],
                          reason="Synthetic explicit withdrawal test")
    revoked.restart()
    revocation = revoked.proof()
    _check(checks, "revocation_survives_restart_and_blocks_review_qualification",
        revocation["state"] == "BLOCKED"
        and revocation["roles"][0]["components"]["approval"]["reason"] == "approval_revoked")
    empty = create_demo(output / "synthetic-empty", capture=False)
    _check(checks, "missing_sources_cannot_manufacture_human_request",
        _refuses(empty.request)
        and empty.review.inspect(empty.principal, empty.scope)["current_request"] is None
        and empty.export()["source_export"]["revision_report"]["human_root_cause_count"] == 0)
    _check(checks, "proof_never_claims_truth_identity_or_execution",
        all(proof[name] is False for name in ("execution_authorized", "source_authenticity_verified",
            "operator_authenticity_verified", "factual_support_verified", "canonical_packet_content_verified"))
        and all(value == 0 for value in proof["effects"].values()))
    evidence = {"schema": "keel.live_rehearsal.v1", "synthetic": True,
        "not_live_evidence": True, "status": "PASS", "fixture_clock": NOW.isoformat(),
        "checks": checks, "checks_passed": sum(row["passed"] for row in checks), "checks_total": len(checks),
        "declared_operational_roles_tested": 0, "synthetic_roles_qualified": 1,
        "model_calls": 0, "http_calls": 0, "browser_actions": 0, "canonical_writes": 0,
        "execution_authorized": False, "source_authenticity_verified": False,
        "operator_authenticity_verified": False, "rendered_preparation": "NOT_RUN",
        "form_readback": "NOT_RUN", "limitations": [
            "All data, people, facts, reviews and decisions are synthetic fixtures.",
            "The positive fixture explicitly authors synthetic canonical dependencies and review artifacts.",
            "Production connectors never author or refresh assurance or trust evidence.",
            "No actual host authentication, upstream live source, model, rendered browser or submission was exercised."]}
    write_private(output / "live-rehearsal.json", evidence)
    write_private(output / "synthetic-capture-review-proof.json", proof)
    write_private(output / "synthetic-workbench-snapshot.json", snapshot)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new synthetic-only output directory")
    args = parser.parse_args(argv)
    report = run_rehearsal(args.out)
    print(json.dumps({name: report[name] for name in ("status", "synthetic", "checks_passed",
                                                   "checks_total", "execution_authorized")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
