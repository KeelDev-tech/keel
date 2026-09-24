"""Recovery proof fails closed on missing application objects or QA evidence."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from keel_workflow.recovery import evaluate_recovery, recovery_scope
from keel_workflow.verification import aggregate_run


BUILD = "a" * 64


def manifest():
    return {"schema_version": 1, "snapshot_id": "backup-1", "build_sha256": BUILD,
            "objects": [{"object_id": "application.documents", "kind": "schema", "version": "3", "sha256": "b" * 64},
                        {"object_id": "application.receipts", "kind": "data", "version": "1", "sha256": "c" * 64}]}


def inventory(expected):
    return {"snapshot_id": expected["snapshot_id"], "build_sha256": expected["build_sha256"],
            "provider_healthy": True, "inventory_complete": True, "objects": copy.deepcopy(expected["objects"]),
            "evidence": [{"ref": "local://restore/inventory", "sha256": "d" * 64}]}


def qa_run(expected, observed, *, statuses=None, check_ids=None):
    statuses = statuses or {}
    check_ids = check_ids if check_ids is not None else ["authorization", "integrity", "synthetic_workflow"]
    spec = {"schema_version": 1, "run_id": "restore-qa-1", "build_sha256": expected["build_sha256"],
            "workflow_scope": recovery_scope(expected, observed),
            "checks": [{"check_id": cid, "depends_on": []} for cid in check_ids]}
    observations = {**{key: spec[key] for key in ("run_id", "build_sha256", "workflow_scope")},
                    "checks": [{"check_id": cid, "status": statuses.get(cid, "PASS"),
                                "finding_code": "observed", "detail": "Local synthetic restore check",
                                "evidence": [{"ref": "local://checks/" + cid, "sha256": "e" * 64}]}
                               for cid in check_ids]}
    return aggregate_run(spec, observations)


class WorkflowRecoveryTests(unittest.TestCase):
    def test_complete_evidenced_restore_ready_for_review_only(self):
        expected = manifest()
        observed = inventory(expected)
        run = qa_run(expected, observed)
        before = copy.deepcopy((expected, observed, run))
        result = evaluate_recovery(expected, observed, run)
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertFalse(result["execution_authorized"])
        self.assertEqual((expected, observed, run), before)

    def test_healthy_provider_with_empty_restored_application_is_not_ready(self):
        expected = manifest()
        observed = inventory(expected)
        observed["objects"] = []
        result = evaluate_recovery(expected, observed, qa_run(expected, observed))
        self.assertFalse(result["ready"])
        self.assertEqual([i["code"] for i in result["issues"]], ["missing_object", "missing_object"])

    def test_empty_expected_manifest_rejected(self):
        expected = manifest()
        expected["objects"] = []
        with self.assertRaises(ValueError): recovery_scope(expected, inventory(expected))

    def test_each_identity_or_inventory_problem_blocks(self):
        for mutation, code in (("snapshot", "snapshot_mismatch"), ("build", "build_mismatch"),
                               ("health", "provider_unhealthy"), ("complete", "inventory_incomplete"),
                               ("evidence", "inventory_evidence_missing"), ("kind", "kind_mismatch"),
                               ("version", "version_mismatch"), ("sha256", "sha256_mismatch"),
                               ("extra", "unexpected_object")):
            with self.subTest(mutation=mutation):
                expected = manifest()
                observed = inventory(expected)
                if mutation == "snapshot": observed["snapshot_id"] = "backup-other"
                if mutation == "build": observed["build_sha256"] = "f" * 64
                if mutation == "health": observed["provider_healthy"] = False
                if mutation == "complete": observed["inventory_complete"] = False
                if mutation == "evidence": observed["evidence"] = []
                if mutation == "kind": observed["objects"][0]["kind"] = "data"
                if mutation == "version": observed["objects"][0]["version"] = "2"
                if mutation == "sha256": observed["objects"][0]["sha256"] = "f" * 64
                if mutation == "extra": observed["objects"].append({**observed["objects"][0], "object_id": "rogue"})
                result = evaluate_recovery(expected, observed, qa_run(expected, observed))
                self.assertFalse(result["ready"])
                self.assertIn(code, {i["code"] for i in result["issues"]})

    def test_required_checks_cannot_be_substituted_with_provider_smoke(self):
        expected = manifest()
        observed = inventory(expected)
        result = evaluate_recovery(expected, observed, qa_run(expected, observed, check_ids=["provider_smoke"]))
        self.assertFalse(result["ready"])
        self.assertEqual(len([i for i in result["issues"] if i["code"] == "required_check_missing"]), 3)

    def test_each_required_check_and_additional_check_must_pass(self):
        expected = manifest()
        observed = inventory(expected)
        check_ids = ["authorization", "integrity", "synthetic_workflow", "privacy"]
        for cid in check_ids:
            for status in ("FAIL", "TOOL_ERROR", "SKIPPED", "FLAKY", "UNVERIFIED"):
                with self.subTest(cid=cid, status=status):
                    run = qa_run(expected, observed, statuses={cid: status}, check_ids=check_ids)
                    result = evaluate_recovery(expected, observed, run)
                    self.assertFalse(result["ready"])
                    self.assertIn("workflow_not_verified", {i["code"] for i in result["issues"]})

    def test_previous_green_checks_cannot_be_reused_for_changed_inventory(self):
        expected = manifest()
        observed = inventory(expected)
        run = qa_run(expected, observed)
        observed["objects"][0]["sha256"] = "f" * 64
        result = evaluate_recovery(expected, observed, run)
        self.assertFalse(result["ready"])
        self.assertIn("verification_scope_mismatch", {i["code"] for i in result["issues"]})

    def test_other_build_green_run_cannot_be_reused(self):
        expected = manifest()
        observed = inventory(expected)
        run = qa_run(expected, observed)
        run["spec"]["build_sha256"] = "f" * 64
        run["observations"]["build_sha256"] = "f" * 64
        run = aggregate_run(run["spec"], run["observations"])
        result = evaluate_recovery(expected, observed, run)
        self.assertIn("verification_build_mismatch", {i["code"] for i in result["issues"]})

    def test_duplicate_objects_and_nonboolean_inventory_flags_rejected(self):
        expected = manifest()
        for mutation in ("duplicate", "health", "complete", "unexpected"):
            with self.subTest(mutation=mutation):
                observed = inventory(expected)
                if mutation == "duplicate": observed["objects"].append(copy.deepcopy(observed["objects"][0]))
                if mutation == "health": observed["provider_healthy"] = 1
                if mutation == "complete": observed["inventory_complete"] = "true"
                if mutation == "unexpected": observed["trusted"] = True
                with self.assertRaises(ValueError): recovery_scope(expected, observed)

    def test_tampered_qa_summary_rejected(self):
        expected = manifest()
        observed = inventory(expected)
        run = qa_run(expected, observed, statuses={"integrity": "FAIL"})
        run["all_pass"] = True
        with self.assertRaises(ValueError): evaluate_recovery(expected, observed, run)

    def test_missing_check_evidence_does_not_verify_recovery(self):
        expected = manifest()
        observed = inventory(expected)
        run = qa_run(expected, observed)
        run["observations"]["checks"][0]["evidence"] = []
        run = aggregate_run(run["spec"], run["observations"])
        self.assertFalse(evaluate_recovery(expected, observed, run)["ready"])

    def test_real_local_file_restore_and_tamper_inventory(self):
        # Actual isolated bytes restored and re-read; does not claim DB recovery.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            content = json.dumps({"documents": [{"id": "synthetic-1", "version": 3}], "receipts": []}).encode()
            snapshot.write_bytes(content)
            restored = root / "restored.json"
            restored.write_bytes(snapshot.read_bytes())
            expected = {"schema_version": 1, "snapshot_id": "local-snapshot", "build_sha256": BUILD,
                        "objects": [{"object_id": "restored.json", "kind": "asset", "version": "1",
                                     "sha256": hashlib.sha256(content).hexdigest()}]}
            observed = inventory(expected)
            observed["objects"][0]["sha256"] = hashlib.sha256(restored.read_bytes()).hexdigest()
            self.assertTrue(evaluate_recovery(expected, observed, qa_run(expected, observed))["ready"])
            restored.write_bytes(b"{}")
            observed["objects"][0]["sha256"] = hashlib.sha256(restored.read_bytes()).hexdigest()
            self.assertFalse(evaluate_recovery(expected, observed, qa_run(expected, observed))["ready"])


if __name__ == "__main__":
    unittest.main()
