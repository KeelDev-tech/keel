"""Execute an offline synthetic workflow and a real local SQLite restore rehearsal.

The review verdicts and host approvals are explicit fixtures, not model output
or authenticated authority. PASS measures this local exercise only. No network,
browser, provider submission, or real applicant data is used.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .delivery import DeliveryError, DeliveryStore, SyntheticAdapter, build_bundle
from .integration import evaluate_candidate, simulate_candidate
from .recovery import evaluate_recovery, recovery_scope
from .reviews import ReviewStore, subject_digest
from .verification import aggregate_run


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _write(root, name, body):
    path = root / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
    return {"ref": name, "sha256": _hash(body)}


def _json(root, name, value):
    return _write(root, name, (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())


def _sql_inventory(path):
    """Measure a closed snapshot without creating absent tables or databases."""
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        schema = [list(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        counts = {}
        for kind, name, _table, _sql in schema:
            if kind == "table":
                # Names come from SQLite metadata, and are quoted as identifiers.
                counts[name] = connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
        return {"integrity_check": integrity, "schema": schema, "row_counts": counts}
    finally:
        connection.close()


def _backup(source, destination):
    """Online backup includes committed WAL data; copying a live DB file does not."""
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    original = sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        original.backup(target)
    finally:
        target.close()
        original.close()


def _objects(directory, names=None):
    if names is None:
        names = [path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()]
    return [{"object_id": name, "kind": "sqlite" if name.endswith(".sqlite") else "artifact",
             "version": "synthetic-snapshot-v1", "sha256": _hash((directory / name).read_bytes())}
            for name in sorted(names)]


def _denied(call, expected_reason):
    try:
        call()
    except (ValueError, DeliveryError) as exc:
        return {"denied": True, "reason": str(exc), "expected_reason": expected_reason,
                "reason_matched": expected_reason in str(exc)}
    return {"denied": False, "reason": "operation unexpectedly accepted", "expected_reason": expected_reason,
            "reason_matched": False}


def _run(build_hash, scope, run_id, checks, evidence, *, dependencies=None):
    dependencies = dependencies or {}
    spec = {"schema_version": 1, "run_id": run_id, "build_sha256": build_hash,
            "workflow_scope": scope,
            "checks": [{"check_id": name, "depends_on": dependencies.get(name, [])} for name in checks]}
    observations = {key: spec[key] for key in ("run_id", "build_sha256", "workflow_scope")}
    observations["checks"] = [
        {"check_id": name, "status": "UNVERIFIED" if passed is None else "PASS" if passed else "FAIL",
         "finding_code": "actual_local_observation", "detail": "Measured synthetic rehearsal result: " + name,
         "evidence": [evidence]} for name, passed in checks.items()]
    return aggregate_run(spec, observations)


def run_demo(new_output_path):
    """Write one new private exercise directory and return its explicit result.

    Existing paths are refused. Operational failure retains an INCOMPLETE report
    where storage remains usable. Failed checks never produce a PASS report.
    """
    from tools.make_workflow_demo import NOW, make_fixture
    from tools.source_inventory import inventory

    root = Path(new_output_path).absolute()
    root.mkdir(mode=0o700, exist_ok=False)
    report = {"schema_version": 1, "status": "INCOMPLETE", "synthetic": True,
              "fixture_time": NOW.isoformat(), "execution_authorized": False, "submitted": False,
              "effects": {"model_calls": 0, "http_requests": 0, "browser_actions": 0,
                          "provider_submissions": 0, "messages_sent": 0},
              "boundary": "Local synthetic workflow and SQLite restore only; fixture verdicts are not real reviews.",
              "evidence": []}
    stores = []
    try:
        build_hash = inventory()["source_sha256"]
        report["build_sha256"] = build_hash
        bundle, flow, assurance, trust = make_fixture()
        fixtures = {"synthetic": True, "flow": flow, "assurance": assurance, "trust": trust,
                    "bundle": bundle.to_dict(), "review_verdict_source": "hard-coded synthetic fixtures"}
        input_ref = _json(root, "inputs.json", fixtures)
        bundle_ref = _write(root, "bundle.json", bundle.canonical_json.encode())
        for name, body in bundle.attachments:
            _write(root, "attachments/" + name, body)
        report["evidence"].extend([input_ref, bundle_ref])
        candidate = evaluate_candidate(bundle, flow_export=flow, assurance_export=assurance,
                                       trust_export=trust, now=NOW)
        reviews = ReviewStore(root / "reviews.sqlite")
        round_id = "synthetic-round"
        subject_hash = subject_digest(candidate["subject"])
        reviewers = candidate["reviewer_ids"]
        reviews.create_round(round_id, candidate["subject"], reviewers, NOW + timedelta(minutes=10), now=NOW)
        first_view = reviews.phase_a(round_id, reviewers[0], subject_hash, now=NOW)
        commitments = [reviews.commit(round_id, reviewers[0], subject_hash, verdict="PASS",
                      covered_claim_ids=candidate["subject"]["required_claim_ids"], now=NOW)]
        second_view = reviews.phase_a(round_id, reviewers[1], subject_hash, now=NOW)
        early_reveal = _denied(lambda: reviews.phase_b(round_id, reviewers[0], subject_hash, now=NOW), "unavailable")
        for reviewer in reviewers[1:]:
            commitments.append(reviews.commit(round_id, reviewer, subject_hash, verdict="PASS",
                               covered_claim_ids=candidate["subject"]["required_claim_ids"], now=NOW))
        reviews.seal(round_id, subject_hash, now=NOW)
        reveals = []
        for reviewer in reviewers:
            reveals.append(reviews.phase_b(round_id, reviewer, subject_hash, now=NOW))
            reviews.audit(round_id, reviewer, subject_hash, verdict="PASS", now=NOW)
        completed_review = reviews.evaluate(round_id, subject_hash, now=NOW)

        authority = "synthetic-fixture-authority"
        delivery = DeliveryStore(root / "delivery.sqlite"); stores.append(delivery)
        approval = delivery.approve(bundle, approver_id="synthetic-human-fixture", authority_ref=authority,
                                    expires_at=NOW + timedelta(minutes=10), now=NOW)

        def simulate(store, approval_id, key, adapter, account=None):
            return simulate_candidate(bundle, flow_export=flow, assurance_export=assurance, trust_export=trust,
                                      reviews=reviews, round_id=round_id, delivery=store, approval_id=approval_id,
                                      idempotency_key=key, adapter=adapter,
                                      current_account_id=account or bundle.document["account_id"],
                                      current_authority_ref=authority, now=NOW)

        simulation = simulate(delivery, approval["approval_id"], "synthetic-once", SyntheticAdapter("confirmed"))
        events_before_restart = delivery.events()
        delivery.close(); stores.remove(delivery)
        delivery = DeliveryStore(root / "delivery.sqlite"); stores.append(delivery)
        restarted = simulate(delivery, approval["approval_id"], "synthetic-once", SyntheticAdapter("confirmed"))
        restart_preserved = (simulation["simulation"] == restarted["simulation"]
                             and events_before_restart == delivery.events()
                             and delivery.load_bundle(bundle.sha256) == bundle)

        unknown = DeliveryStore(root / "unknown.sqlite"); stores.append(unknown)
        unknown_approval = unknown.approve(bundle, approver_id="synthetic-human-fixture", authority_ref=authority,
                                          expires_at=NOW + timedelta(minutes=10), now=NOW)
        timed_out = simulate(unknown, unknown_approval["approval_id"], "synthetic-timeout", SyntheticAdapter("timeout"))
        retry = _denied(lambda: simulate(unknown, unknown_approval["approval_id"], "synthetic-retry", SyntheticAdapter("confirmed")), "UNKNOWN")
        timeout_preserved = (timed_out["simulation"]["status"] == "UNKNOWN" and retry["reason_matched"]
                             and unknown.get_attempt(timed_out["simulation"]["attempt_id"])["status"] == "UNKNOWN"
                             and unknown.connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 1)
        changed_document = deepcopy(bundle.document)
        changed_document["content"]["application"]["answers"]["name"] = "Different Synthetic Applicant"
        changed = build_bundle(**{key: changed_document[key] for key in (
            "workspace_id", "role_id", "action", "destination", "account_id", "revisions", "content")},
            attachments=dict(bundle.attachments))
        changed_rejection = _denied(lambda: delivery.run_simulation(
            changed, approval_id=approval["approval_id"], idempotency_key="synthetic-changed",
            adapter=SyntheticAdapter("confirmed"), current_revisions=changed.document["revisions"],
            current_account_id=changed.document["account_id"], current_authority_ref=authority, now=NOW), "Approval is not bound")

        measurements = {"candidate": candidate, "phase_a_views": [first_view, second_view],
                        "early_reveal": early_reveal, "commitments": commitments, "reveals": reveals,
                        "review": completed_review, "simulation": simulation, "restarted": restarted,
                        "events_before_restart": events_before_restart, "events_after_restart": delivery.events(),
                        "timed_out": timed_out, "retry_denial": retry, "changed_bundle_denial": changed_rejection,
                        "synthetic": True, "real_model_evaluation": False}
        checks = {"candidate_gates": candidate["state"] == "READY_FOR_BLIND_REVIEW",
                  "blind_review": (early_reveal["reason_matched"] and "commitments" not in second_view
                                   and completed_review["state"] == "READY_FOR_HUMAN_REVIEW"
                                   and all(len(row["commitments"]) == len(reviewers) for row in reveals)),
                  "simulation": (simulation["simulation"]["status"] == "SIMULATED_CONFIRMED"
                                 and simulation["simulation"]["submitted"] is False
                                 and simulation["simulation"]["receipt"]["bundle_sha256"] == bundle.sha256),
                  "restart_idempotency": restart_preserved, "unknown_retry_hold": timeout_preserved,
                  "changed_bundle_denied": changed_rejection["reason_matched"]}
        measurement_ref = _json(root, "evidence/workflow-observations.json", {"checks": checks, **measurements})
        workflow_run = _run(build_hash, "synthetic-local-workflow:" + bundle.sha256,
                            "synthetic-workflow", checks, measurement_ref)
        report["workflow"] = workflow_run
        report["evidence"].append(measurement_ref)
        _json(root, "workflow-verification.json", workflow_run)
        for store in list(stores):
            store.close(); stores.remove(store)

        backup = root / "backup"; backup.mkdir(mode=0o700)
        restored = root / "restored"; restored.mkdir(mode=0o700)
        database_names = ["reviews.sqlite", "delivery.sqlite", "unknown.sqlite"]
        for name in database_names:
            _backup(root / name, backup / name)
        file_names = database_names + ["bundle.json", "inputs.json"]
        file_names += ["attachments/" + name for name, _body in bundle.attachments]
        for name in file_names:
            if name not in database_names:
                _write(backup, name, (root / name).read_bytes())
            _write(restored, name, (backup / name).read_bytes())
        expected = {"schema_version": 1, "snapshot_id": "synthetic-local-snapshot", "build_sha256": build_hash,
                    "objects": _objects(backup)}
        backup_sql = {name: _sql_inventory(backup / name) for name in database_names}
        restored_sql = {name: _sql_inventory(restored / name) for name in database_names}

        restored_reviews = ReviewStore(restored / "reviews.sqlite")
        restored_review = restored_reviews.evaluate(round_id, subject_hash, now=NOW)
        restored_delivery = DeliveryStore(restored / "delivery.sqlite"); stores.append(restored_delivery)
        restored_unknown = DeliveryStore(restored / "unknown.sqlite"); stores.append(restored_unknown)
        restored_attempt = restored_delivery.get_attempt(simulation["simulation"]["attempt_id"])
        restored_held_attempt = restored_unknown.get_attempt(timed_out["simulation"]["attempt_id"])
        restored_bundle = restored_delivery.load_bundle(bundle.sha256)
        denied_account = _denied(lambda: restored_delivery.run_simulation(
            bundle, approval_id=approval["approval_id"], idempotency_key="restored-account-change",
            adapter=SyntheticAdapter("confirmed"), current_revisions=bundle.document["revisions"],
            current_account_id="different-synthetic-account", current_authority_ref=authority, now=NOW), "Account identity changed")
        restored_result = simulate_candidate(bundle, flow_export=flow, assurance_export=assurance, trust_export=trust,
            reviews=restored_reviews, round_id=round_id, delivery=restored_delivery, approval_id=approval["approval_id"],
            idempotency_key="synthetic-once", adapter=SyntheticAdapter("confirmed"),
            current_account_id=bundle.document["account_id"], current_authority_ref=authority, now=NOW)
        for store in list(stores):
            store.close(); stores.remove(store)
        actual_objects = _objects(restored)
        recovery_checks = {
            "integrity": (expected["objects"] == actual_objects and backup_sql == restored_sql
                          and all(row["integrity_check"] == ["ok"] for row in restored_sql.values())
                          and (restored / "bundle.json").read_bytes() == bundle.canonical_json.encode()
                          and all((restored / "attachments" / name).read_bytes() == body for name, body in bundle.attachments)),
            "authorization": denied_account["reason_matched"],
            "synthetic_workflow": (restored_review == completed_review and restored_bundle == bundle
                                   and restored_attempt == simulation["simulation"]
                                   and restored_held_attempt["status"] == "UNKNOWN"
                                   and restored_result["simulation"] == simulation["simulation"]),
        }
        restore_ref = _json(root, "evidence/restore-observations.json", {
            "synthetic": True, "checks": recovery_checks, "backup_sql": backup_sql, "restored_sql": restored_sql,
            "restored_review": restored_review, "restored_attempt": restored_attempt,
            "restored_held_attempt": restored_held_attempt, "changed_account_denial": denied_account,
            "restored_bundle_sha256": restored_bundle.sha256, "restored_simulation": restored_result})
        observed = {"snapshot_id": expected["snapshot_id"], "build_sha256": build_hash,
                    "provider_healthy": all(row["integrity_check"] == ["ok"] for row in restored_sql.values()),
                    "inventory_complete": True, "objects": actual_objects, "evidence": [restore_ref]}
        recovery_run = _run(build_hash, recovery_scope(expected, observed), "synthetic-restore", recovery_checks, restore_ref,
                            dependencies={"authorization": ["integrity"], "synthetic_workflow": ["authorization"]})
        recovered = evaluate_recovery(expected, observed, recovery_run)
        report["recovery"] = recovered
        report["evidence"].extend([restore_ref, _json(root, "recovery-expected.json", expected),
                                   _json(root, "recovery-observed.json", observed),
                                   _json(root, "recovery-verification.json", recovery_run)])

        empty = root / "empty-restore"; empty.mkdir(mode=0o700)
        descriptor = os.open(empty / "empty.sqlite", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600); os.close(descriptor)
        with sqlite3.connect(empty / "empty.sqlite") as connection:
            # Materialize a valid empty database header without application objects.
            connection.execute("VACUUM")
        empty_sql = _sql_inventory(empty / "empty.sqlite")
        empty_checks = {"integrity": empty_sql["integrity_check"] == ["ok"], "authorization": None,
                        "synthetic_workflow": bool(empty_sql["row_counts"].get("workflow_attempts", 0))}
        empty_ref = _json(root, "evidence/empty-restore-observations.json", {
            "sqlite": empty_sql, "checks": empty_checks, "reason": "Application objects and approvals are absent."})
        empty_observed = {**observed, "provider_healthy": empty_checks["integrity"], "objects": [], "evidence": [empty_ref]}
        empty_run = _run(build_hash, recovery_scope(expected, empty_observed), "synthetic-empty-restore", empty_checks, empty_ref)
        empty_result = evaluate_recovery(expected, empty_observed, empty_run)
        report["empty_restore"] = empty_result
        report["evidence"].extend([empty_ref, _json(root, "empty-recovery-observed.json", empty_observed),
                                   _json(root, "empty-recovery-verification.json", empty_run)])
        report["status"] = "PASS" if (workflow_run["all_pass"] and recovered["ready"]
            and not empty_result["ready"] and any(row["code"] == "missing_object" for row in empty_result["issues"])) else "FAIL"
    except Exception as exc:
        report["status"] = "INCOMPLETE"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:4096]}
    finally:
        for store in stores:
            store.close()
    _json(root, "report.json", report)
    return report
