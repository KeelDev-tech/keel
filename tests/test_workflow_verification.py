"""Workflow proof and root-cause regression tests."""
import copy
import unittest

from keel_workflow.verification import aggregate_run, compare_runs, validate_run


BUILD = "a" * 64


def spec(checks=None, run_id="run-1"):
    return {"schema_version": 1, "run_id": run_id, "build_sha256": BUILD,
            "workflow_scope": "synthetic-application:fixture-1",
            "checks": checks if checks is not None else [
                {"check_id": "discover", "depends_on": []},
                {"check_id": "prepare", "depends_on": ["discover"]},
                {"check_id": "review", "depends_on": ["prepare"]},
                {"check_id": "receipt", "depends_on": ["review"]}]}


def observations(contract, statuses=None):
    statuses = statuses or {}
    return {**{key: contract[key] for key in ("run_id", "build_sha256", "workflow_scope")},
            "checks": [{"check_id": check["check_id"], "status": statuses.get(check["check_id"], "PASS"),
                        "evidence": [{"ref": "local://" + check["check_id"], "sha256": "b" * 64}],
                        "finding_code": "observed", "detail": "Synthetic adapter observation"}
                       for check in contract["checks"]]}


class WorkflowVerificationTests(unittest.TestCase):
    def test_complete_workflow_pass_and_no_mutation(self):
        contract = spec()
        envelope = observations(contract)
        original = copy.deepcopy((contract, envelope))
        run = aggregate_run(contract, envelope)
        self.assertTrue(run["all_pass"])
        self.assertTrue(run["complete"])
        self.assertEqual(run["status"], "PASS")
        self.assertFalse(run["execution_authorized"])
        self.assertEqual((contract, envelope), original)
        run["spec"]["checks"][0]["depends_on"].append("rogue")
        self.assertEqual((contract, envelope), original)

    def test_provider_check_is_not_full_workflow(self):
        contract = spec()
        envelope = observations(contract)
        envelope["checks"] = envelope["checks"][:1]
        run = aggregate_run(contract, envelope)
        self.assertFalse(run["all_pass"])
        self.assertFalse(run["complete"])
        self.assertEqual(run["counts"]["UNVERIFIED"], 1)
        self.assertEqual(run["counts"]["BLOCKED"], 2)
        self.assertEqual(len(run["root_findings"]), 1)

    def test_pass_and_fail_without_evidence_are_unverified(self):
        for reported in ("PASS", "FAIL"):
            with self.subTest(reported=reported):
                contract = spec()
                envelope = observations(contract, {"discover": reported})
                envelope["checks"][0]["evidence"] = []
                run = aggregate_run(contract, envelope)
                self.assertEqual(run["checks"][0]["status"], "UNVERIFIED")
                self.assertEqual(run["status"], "INCOMPLETE")
                self.assertEqual(run["counts"]["FAIL"], 0)

    def test_root_failure_is_counted_once_for_diamond_cascade(self):
        contract = spec([
            {"check_id": "connection", "depends_on": []},
            {"check_id": "documents", "depends_on": ["connection"]},
            {"check_id": "permissions", "depends_on": ["connection"]},
            {"check_id": "submit", "depends_on": ["documents", "permissions"]},
        ])
        run = aggregate_run(contract, observations(contract, {"connection": "FAIL", "submit": "FAIL"}))
        self.assertEqual(run["counts"]["FAIL"], 1)
        self.assertEqual(run["counts"]["BLOCKED"], 3)
        self.assertEqual(len(run["root_findings"]), 1)
        root = run["root_findings"][0]["root_id"]
        self.assertTrue(all(row["root_ids"] == [root] for row in run["checks"]))
        submit = next(row for row in run["checks"] if row["check_id"] == "submit")
        self.assertEqual(submit["reported_status"], "FAIL")

    def test_two_independent_failures_not_collapsed_by_same_code(self):
        contract = spec([{"check_id": cid, "depends_on": []} for cid in ("authorization", "documents")])
        run = aggregate_run(contract, observations(contract, {cid: "FAIL" for cid in ("authorization", "documents")}))
        self.assertEqual(len(run["root_findings"]), 2)
        self.assertEqual(run["counts"]["FAIL"], 2)

    def test_tool_error_never_becomes_product_failure(self):
        contract = spec()
        run = aggregate_run(contract, observations(contract, {"discover": "TOOL_ERROR"}))
        self.assertEqual(run["status"], "INCOMPLETE")
        self.assertEqual(run["counts"]["TOOL_ERROR"], 1)
        self.assertEqual(run["counts"]["FAIL"], 0)
        self.assertEqual(run["counts"]["BLOCKED"], 3)

    def test_skipped_flaky_unknown_and_unexplained_block_cannot_pass(self):
        for state in ("SKIPPED", "FLAKY", "UNVERIFIED", "BLOCKED"):
            with self.subTest(state=state):
                contract = spec()
                run = aggregate_run(contract, observations(contract, {"discover": state}))
                self.assertFalse(run["all_pass"])
                self.assertFalse(run["complete"])
                self.assertEqual(len(run["root_findings"]), 1)

    def test_empty_observed_workflow_is_not_pass(self):
        contract = spec()
        envelope = observations(contract)
        envelope["checks"] = []
        run = aggregate_run(contract, envelope)
        self.assertFalse(run["all_pass"])
        self.assertEqual(run["counts"]["UNVERIFIED"], 1)
        self.assertEqual(run["counts"]["BLOCKED"], 3)

    def test_empty_expected_workflow_is_invalid(self):
        contract = spec([])
        with self.assertRaises(ValueError):
            aggregate_run(contract, observations(contract))

    def test_mismatched_run_build_or_scope_is_invalid(self):
        contract = spec()
        for field, value in (("run_id", "other"), ("build_sha256", "c" * 64), ("workflow_scope", "production")):
            with self.subTest(field=field):
                envelope = observations(contract)
                envelope[field] = value
                with self.assertRaises(ValueError):
                    aggregate_run(contract, envelope)

    def test_malformed_or_cyclic_dependencies_are_invalid(self):
        bad_specs = [
            [{"check_id": "x", "depends_on": ["missing"]}],
            [{"check_id": "x", "depends_on": ["x"]}],
            [{"check_id": "x", "depends_on": ["y"]}, {"check_id": "y", "depends_on": ["x"]}],
            [{"check_id": "x", "depends_on": []}, {"check_id": "x", "depends_on": []}],
            [{"check_id": "x", "depends_on": []}, {"check_id": "y", "depends_on": ["x", "x"]}],
            [{"check_id": "x", "depends_on": "x"}],
        ]
        for checks in bad_specs:
            with self.subTest(checks=checks):
                contract = spec(checks)
                with self.assertRaises(ValueError):
                    aggregate_run(contract, observations(contract))

    def test_unknown_duplicate_malformed_observation_rejected(self):
        contract = spec()
        for mutation in ("unknown", "duplicate", "status", "digest", "unknown_field", "code"):
            with self.subTest(mutation=mutation):
                envelope = observations(contract)
                if mutation == "unknown": envelope["checks"][0]["check_id"] = "other"
                if mutation == "duplicate": envelope["checks"].append(copy.deepcopy(envelope["checks"][0]))
                if mutation == "status": envelope["checks"][0]["status"] = ["PASS"]
                if mutation == "digest": envelope["checks"][0]["evidence"][0]["sha256"] = "bad"
                if mutation == "unknown_field": envelope["checks"][0]["override"] = True
                if mutation == "code": envelope["checks"][0]["finding_code"] = " "
                with self.assertRaises(ValueError): aggregate_run(contract, envelope)

    def test_same_evidence_ref_with_conflicting_digest_rejected(self):
        contract = spec()
        envelope = observations(contract)
        envelope["checks"][1]["evidence"][0] = {"ref": "local://discover", "sha256": "c" * 64}
        with self.assertRaises(ValueError): aggregate_run(contract, envelope)

    def test_dag_declaration_order_does_not_change_result(self):
        contract = spec()
        envelope = observations(contract)
        expected = aggregate_run(contract, envelope)
        contract["checks"].reverse()
        envelope["checks"].reverse()
        self.assertEqual(aggregate_run(contract, envelope), expected)

    def test_tampered_snapshot_rejected(self):
        contract = spec()
        run = aggregate_run(contract, observations(contract, {"discover": "FAIL"}))
        run["all_pass"] = True
        with self.assertRaises(ValueError): validate_run(run)

    def test_snapshot_type_tampering_rejected(self):
        contract = spec()
        run = aggregate_run(contract, observations(contract))
        run["all_pass"] = 1
        with self.assertRaises(ValueError): validate_run(run)

    def test_comparison_new_recurring_and_resolved(self):
        checks = [{"check_id": cid, "depends_on": []} for cid in ("a", "b", "c")]
        old_spec, new_spec = spec(checks), spec(checks, "run-2")
        old = aggregate_run(old_spec, observations(old_spec, {"a": "FAIL", "b": "FAIL"}))
        new = aggregate_run(new_spec, observations(new_spec, {"b": "FAIL", "c": "TOOL_ERROR"}))
        before = copy.deepcopy((old, new))
        comparison = compare_runs(old, new)
        self.assertEqual(len(comparison["new"]), 1)
        self.assertEqual(len(comparison["recurring"]), 1)
        self.assertEqual(len(comparison["resolved"]), 1)
        self.assertEqual(comparison["unresolved"], [])
        self.assertEqual(comparison["flaky"], [])
        self.assertEqual((old, new), before)

    def test_missing_check_does_not_resolve_old_failure(self):
        first, second = spec(), spec(run_id="run-2")
        old = aggregate_run(first, observations(first, {"prepare": "FAIL"}))
        new = aggregate_run(second, observations(second, {"discover": "TOOL_ERROR"}))
        comparison = compare_runs(old, new)
        self.assertEqual(comparison["resolved"], [])
        self.assertEqual(len(comparison["unresolved"]), 1)

    def test_comparison_preserves_explicit_flakiness(self):
        first, second = spec(), spec(run_id="run-2")
        old = aggregate_run(first, observations(first))
        new = aggregate_run(second, observations(second, {"receipt": "FLAKY"}))
        self.assertEqual(compare_runs(old, new)["flaky"], ["receipt"])

    def test_comparison_rejects_different_build_scope_check_graph_and_same_run(self):
        first = spec()
        old = aggregate_run(first, observations(first))
        for field in ("build_sha256", "workflow_scope", "checks", "run_id"):
            with self.subTest(field=field):
                second = spec(run_id="run-2")
                if field == "build_sha256": second[field] = "f" * 64
                if field == "workflow_scope": second[field] = "another-workflow"
                if field == "checks": second[field][-1]["depends_on"] = ["discover"]
                if field == "run_id": second[field] = first[field]
                new = aggregate_run(second, observations(second))
                with self.assertRaises(ValueError): compare_runs(old, new)

    def test_explicit_cross_build_comparison_tracks_observed_repair(self):
        first, second = spec(), spec(run_id="run-2")
        second["build_sha256"] = "f" * 64
        old = aggregate_run(first, observations(first, {"discover": "FAIL"}))
        new = aggregate_run(second, observations(second))
        originals = copy.deepcopy((old, new))
        with self.assertRaises(ValueError): compare_runs(old, new)
        result = compare_runs(old, new, allow_build_change=True)
        self.assertEqual(result["comparison_scope"], "CROSS_BUILD")
        self.assertEqual(result["previous_build_sha256"], BUILD)
        self.assertEqual(result["current_build_sha256"], "f" * 64)
        self.assertFalse(result["build_change_causality_established"])
        self.assertEqual(result["resolved"], [old["root_findings"][0]["root_id"]])
        self.assertEqual((result["previous_run"], result["current_run"]), originals)
        result["previous_run"]["all_pass"] = True
        self.assertEqual((old, new), originals)

    def test_cross_build_opt_in_never_relaxes_scope_or_check_graph(self):
        first = spec()
        old = aggregate_run(first, observations(first))
        for changed_field in ("scope", "dependency", "coverage"):
            with self.subTest(changed_field=changed_field):
                second = spec(run_id="run-2")
                second["build_sha256"] = "f" * 64
                if changed_field == "scope": second["workflow_scope"] = "different-application"
                if changed_field == "dependency": second["checks"][-1]["depends_on"] = ["discover"]
                if changed_field == "coverage": second["checks"].pop()
                new = aggregate_run(second, observations(second))
                with self.assertRaises(ValueError): compare_runs(old, new, allow_build_change=True)

    def test_cross_build_missing_evidence_cannot_claim_resolution(self):
        first, second = spec(), spec(run_id="run-2")
        second["build_sha256"] = "f" * 64
        old = aggregate_run(first, observations(first, {"receipt": "FAIL"}))
        new_observations = observations(second)
        new_observations["checks"][-1]["evidence"] = []
        new = aggregate_run(second, new_observations)
        result = compare_runs(old, new, allow_build_change=True)
        self.assertEqual(result["resolved"], [])
        self.assertEqual(result["unresolved"], [old["root_findings"][0]["root_id"]])

    def test_same_build_opt_in_is_labelled_same_build_and_requires_boolean(self):
        first, second = spec(), spec(run_id="run-2")
        old = aggregate_run(first, observations(first))
        new = aggregate_run(second, observations(second))
        self.assertEqual(compare_runs(old, new, allow_build_change=True)["comparison_scope"], "SAME_BUILD")
        for invalid in (1, "true", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError): compare_runs(old, new, allow_build_change=invalid)


if __name__ == "__main__":
    unittest.main()
