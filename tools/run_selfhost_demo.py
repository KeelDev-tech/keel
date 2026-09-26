#!/usr/bin/env python3
"""Reproduce the synthetic self-hosted runtime and SQLite restore rehearsal.

All reviewer outputs and observations here are explicitly synthetic fixtures.
There are zero actual inference requests, browsers, paid services or external
submissions. The exercise measures implementation behavior, not model quality.
"""
import argparse
from base64 import b64encode
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_agent.io import write_private
from keel_agent.models import HTTPResult
from keel_agent.runtime import LocalAgent, config_binding, pack_bundle, roster, unpack_bundle
from keel_agent.scope import preflight_candidate
from keel_flow.common import digest
from tools.selfhost_fixture import NOW, make_fixture
from tools.source_inventory import inventory


def _new_bytes(path, body):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def _backup(source, target):
    _new_bytes(target, b"")
    original = sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        original.backup(destination)
    finally:
        destination.close()
        original.close()


def _sql_inventory(path):
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    try:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {"integrity": [row[0] for row in db.execute("PRAGMA integrity_check")],
                "counts": {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                           for name in names}}
    finally:
        db.close()


def run_demo(output):
    root = Path(output).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"schema_version": 1, "status": "INCOMPLETE", "synthetic": True,
              "fixture_start": NOW.isoformat(), "execution_authorized": False,
              "model_quality_validated": False, "checks": {}, "evidence": [],
              "effects": {"actual_model_inference_calls": 0, "external_http_requests": 0,
                          "browser_actions": 0, "external_submissions": 0, "messages_sent": 0},
              "boundary": "Injected synthetic reviewer responses and simulated host clock; actual local database I/O."}
    try:
        source = inventory()
        report.update(source_sha256=source["source_sha256"], source_files=source["source_files"])
        bundle, flow, assurance, trust = make_fixture()
        instant = [NOW]
        clock = lambda: instant[0]
        agent = LocalAgent(root / "host", trust["workspace_id"], clock=clock)
        config = {"reviewers": [
            {"reviewer_id": row["reviewer_id"], "backend": "ollama",
             "endpoint": "http://127.0.0.1:11434/api/chat", "model": "synthetic-model-" + str(index)}
            for index, row in enumerate(assurance["reviewers"])]}
        inputs = {"synthetic": True, "bundle": pack_bundle(bundle), "flow": flow, "assurance": assurance,
                  "trust": trust, "config": config}
        write_private(root / "synthetic-inputs.json", inputs)

        def imported(sequence):
            signed = agent.state.sign_snapshot({"flow": flow, "assurance": assurance, "trust": trust},
                flow["source_revision"], sequence, clock(), clock() + timedelta(seconds=90))
            agent.state.import_snapshot(signed, now=clock())
            return {"sequence": sequence, "envelope_sha256": digest(signed), "body_sha256": signed["body_sha256"]}

        observations = {"snapshots": [imported(1)]}
        job = agent.enqueue_review(bundle, config, idempotency_key="synthetic-review-once", round_id="synthetic-round")
        duplicate = agent.enqueue_review(bundle, config, idempotency_key="synthetic-review-once", round_id="synthetic-round")
        calls = []

        def injected(configured, request, timeout):
            view = json.loads(request["messages"][1]["content"])
            calls.append({"reviewer_id": configured.reviewer_id, "phase": view["phase"],
                          "sees_other_commitments": "immutable_phase_a_commitments" in view,
                          "timeout_seconds": timeout})
            instant[0] += timedelta(seconds=30)
            verdict = {"verdict": "PASS", "covered_claim_ids": view["subject"]["required_claim_ids"], "findings": []}
            body = {"model": configured.model, "done": True, "done_reason": "stop",
                    "created_at": clock().isoformat(),
                    "message": {"role": "assistant", "content": json.dumps(verdict)}}
            return HTTPResult(200, {"content-type": "application/json"}, json.dumps(body).encode())

        completed = agent.worker_once("synthetic-worker", transport=injected)
        try:
            agent.preflight(job["job_id"])
            stale_denied = False
        except ValueError as exc:
            stale_denied = "stale" in str(exc)
            observations["stale_preflight_denial"] = str(exc)
        original_authority = deepcopy(assurance["actions"][0]["authority"])
        # TEST ONLY: emulate a genuinely new unchanged source observation. No
        # production module refreshes or manufactures records in this way.
        stamp = clock().isoformat()
        flow["observed_at"] = flow["pool"]["observed_at"] = flow["capacity"]["observed_at"] = stamp
        for row in flow["leads"]:
            row["observed_at"] = stamp
        for row in flow["discovery"]["sources"]:
            row["observed_at"] = stamp
        assurance["observed_at"] = stamp
        assurance["export_sha256"] = digest(flow)
        trust["flow_export"] = deepcopy(flow)
        trust["observed_at"] = trust["evidence_export"]["observed_at"] = stamp
        observations["snapshots"].append(imported(2))
        fresh = agent.preflight(job["job_id"])
        changed = config_binding(roster(config))
        changed[next(iter(changed))] = digest("replacement-synthetic-model")
        changed_config = preflight_candidate(bundle, **agent.context(), now=clock(),
            lead_anchor=job["payload"]["candidate"]["lead_anchor"], reviews=agent.reviews,
            round_id="synthetic-round", reviewer_config_sha256=changed)
        tampered = pack_bundle(bundle)
        tampered["attachments"][next(iter(tampered["attachments"]))] = b64encode(b"changed synthetic bytes").decode()
        try:
            unpack_bundle(tampered)
            bytes_denied = False
        except ValueError:
            bytes_denied = True

        restored = root / "restored"
        restored.mkdir(mode=0o700)
        names = ("agent.sqlite3", "reviews.sqlite3")
        for name in names:
            _backup(agent.home / name, restored / name)
        _new_bytes(restored / "agent.sqlite3.key", agent.state.key_path.read_bytes())
        before = {name: _sql_inventory(agent.home / name) for name in names}
        restored_before_open = {name: _sql_inventory(restored / name) for name in names}
        reopened = LocalAgent(restored, trust["workspace_id"], clock=clock)
        restored_preflight = reopened.preflight(job["job_id"])
        idle = reopened.worker_once("synthetic-restored-worker", transport=injected)
        restored_job = reopened.state.get_job(job["job_id"])
        checks = {
            "queue_idempotency": duplicate["job_id"] == job["job_id"],
            "new_review_without_old_pass_opinions": all(not row["reviews"] for row in assurance["actions"]),
            "blind_phase_order": [row["phase"] for row in calls] == ["A", "A", "B", "B"]
                                and [row["sees_other_commitments"] for row in calls] == [False, False, True, True],
            "review_finished_after_90_seconds": (clock() - NOW).total_seconds() == 120
                and completed["result"]["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW",
            "stale_preflight_denied": stale_denied,
            "fresh_preflight_passed": fresh["state"] == "PREFLIGHT_CHECKS_PASSED"
                and fresh["candidate"]["subject_sha256"] == job["material_hash"],
            "original_authority_unchanged": original_authority == assurance["actions"][0]["authority"],
            "reviewer_configuration_change_denied": changed_config["state"] == "BLOCKED"
                and any("subject_changed" in reason for reason in changed_config["reasons"]),
            "attachment_bytes_change_denied": bytes_denied,
            "sqlite_restore_integrity": before == restored_before_open
                and all(item["integrity"] == ["ok"] for item in restored_before_open.values()),
            "restored_workflow_passed": restored_preflight["state"] == "PREFLIGHT_CHECKS_PASSED"
                and restored_job == completed,
            "restored_job_not_replayed": idle["state"] == "IDLE" and len(calls) == 4,
            "no_execution_authorization": fresh["execution_authorized"] is False
                and completed["result"]["execution_authorized"] is False,
        }
        observations.update(calls=calls, completed_job_id=job["job_id"], material_hash=job["material_hash"],
            completed_review=completed["result"]["evaluation"], fresh_preflight_state=fresh["state"],
            changed_config_reasons=changed_config["reasons"], source_database=before,
            restored_database=restored_before_open, restored_preflight_state=restored_preflight["state"],
            host_events=agent.state.events(), synthetic=True)
        write_private(root / "observations.json", observations)
        report.update(status="PASS" if all(checks.values()) else "FAIL", checks=checks,
                      synthetic_transport_responses=len(calls), fixture_end=clock().isoformat())
        for name in ("synthetic-inputs.json", "observations.json"):
            report["evidence"].append({"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()})
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
    write_private(root / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new private output directory")
    args = parser.parse_args()
    result = run_demo(args.out)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
