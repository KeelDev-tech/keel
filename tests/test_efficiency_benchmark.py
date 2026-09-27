"""Resource conservation and quality/provenance tests for efficiency replay."""
from copy import deepcopy
import unittest

from keel_efficiency.benchmark import RESOURCES, compare_profiles, run_benchmark


class EfficiencyBenchmarkTests(unittest.TestCase):
    def fixture(self):
        tasks = [{"task_id": "a", "input": [1, 2], "expected": 3},
                 {"task_id": "b", "input": [3, 4], "expected": 7}]
        profiles = {"base": [], "candidate": []}
        for name in profiles:
            for task in tasks:
                profiles[name].append({"task_id": task["task_id"], "claimed_success": True,
                                       "output": task["expected"], "attempts": [{
                                           "attempt_id": task["task_id"], "status": "success",
                                           "resources": dict(zip(RESOURCES, (1, 10, 2, 0, 4, 1))),
                                           "latency_ms": 5}]})
        return {"tasks": tasks, "profiles": profiles, "budget": {"external_credit_micros": 100},
                "validator": lambda task, output: type(output) is int and output == task["expected"],
                "validator_id": "sum-v1", "provenance": "synthetic", "source_id": "fixture-v1"}

    def test_demo_is_replayable_and_explicitly_synthetic(self):
        report = run_benchmark()
        self.assertEqual(report, run_benchmark())
        self.assertTrue(report["demo"])
        self.assertEqual(report["measurement_trust"], "synthetic estimates")
        self.assertFalse(report["production_savings_proven"])
        self.assertFalse(report["replay_dispatches_model_calls"])
        baseline = report["profiles"]["baseline"]
        candidate = report["profiles"]["context_and_reuse"]
        self.assertEqual(baseline["counts"]["verified_successes"], 4)
        self.assertEqual(baseline["counts"]["attempts"], 5)
        self.assertEqual(candidate["counts"]["reused_results"], 1)
        self.assertEqual(baseline["resources"]["known_totals"]["input_tokens"], 1500)
        self.assertEqual(candidate["resources"]["known_totals"]["input_tokens"], 320)
        self.assertIsNone(report["comparisons"][0]["resource_reduction_percent"]["external_credit_micros"])

    def test_failed_retry_cost_included(self):
        options = self.fixture()
        failed = deepcopy(options["profiles"]["candidate"][0]["attempts"][0])
        failed.update(attempt_id="retry-failed", status="failed")
        options["profiles"]["candidate"][0]["attempts"].insert(0, failed)
        report = run_benchmark(**options)["profiles"]["candidate"]
        self.assertEqual(report["resources"]["known_totals"]["external_credit_micros"], 12)
        self.assertEqual(report["resources"]["per_verified_success"]["external_credit_micros"], 6)
        self.assertEqual(report["counts"]["failed_attempts"], 1)
        self.assertEqual(report["counts"]["retry_attempts"], 1)

    def test_model_claim_cannot_create_verified_success(self):
        options = self.fixture()
        options["profiles"]["candidate"][0]["output"] = 999
        report = run_benchmark(**options)
        self.assertEqual(report["profiles"]["candidate"]["counts"]["verified_successes"], 1)
        self.assertEqual(report["profiles"]["candidate"]["counts"]["false_passes"], 1)
        comparison = report["comparisons"][0]
        self.assertEqual(comparison["paired_correctness"]["baseline_only_verified"], 1)
        self.assertIsNone(comparison["resource_reduction_percent"])

    def test_zero_success_never_divides_or_rewards_abstention(self):
        options = self.fixture()
        for traces in options["profiles"].values():
            for trace in traces:
                trace["claimed_success"] = False
        report = run_benchmark(**options)
        self.assertEqual(report["profiles"]["base"]["quality"]["answer_coverage"], 0)
        self.assertIsNone(report["profiles"]["base"]["quality"]["correctness_among_claims"])
        self.assertTrue(all(value is None for value in report["profiles"]["base"]["resources"]["per_verified_success"].values()))
        self.assertIsNone(report["comparisons"][0]["resource_reduction_percent"])

    def test_equal_aggregate_scores_do_not_hide_different_task_errors(self):
        options = self.fixture()
        options["profiles"]["base"][0]["output"] = -1
        options["profiles"]["candidate"][1]["output"] = -1
        report = run_benchmark(**options)
        comparison = report["comparisons"][0]
        self.assertEqual(comparison["verified_success_delta"], 0)
        self.assertFalse(comparison["equal_task_outcomes"])
        self.assertEqual(comparison["paired_correctness"]["baseline_only_verified"], 1)
        self.assertEqual(comparison["paired_correctness"]["candidate_only_verified"], 1)
        self.assertIsNone(comparison["resource_reduction_percent"])

    def test_matching_false_passes_do_not_qualify_for_savings_claim(self):
        options = self.fixture()
        for traces in options["profiles"].values():
            traces[0]["output"] = -1
        report = run_benchmark(**options)
        self.assertTrue(report["comparisons"][0]["equal_task_outcomes"])
        self.assertIsNone(report["comparisons"][0]["resource_reduction_percent"])

    def test_unknown_usage_retains_known_lower_bound_without_savings(self):
        options = self.fixture()
        attempt = options["profiles"]["candidate"][0]["attempts"][0]
        attempt["resources"]["external_credit_micros"] = None
        attempt["status"] = "unknown"
        attempt["latency_ms"] = None
        report = run_benchmark(**options)
        candidate = report["profiles"]["candidate"]
        self.assertEqual(candidate["resources"]["known_totals"]["external_credit_micros"], 4)
        self.assertEqual(candidate["budget_status"], "UNKNOWN")
        self.assertIsNone(candidate["resources"]["per_verified_success"]["external_credit_micros"])
        self.assertIsNone(report["comparisons"][0]["resource_reduction_percent"])
        self.assertIsNone(report["comparisons"][0]["sum_attempt_latency_delta_ms"])

    def test_budget_breach_is_visible_and_not_discarded(self):
        options = self.fixture()
        options["budget"]["external_credit_micros"] = 1
        report = run_benchmark(**options)
        self.assertEqual(report["profiles"]["candidate"]["budget_status"], "EXCEEDED")
        self.assertEqual(report["profiles"]["candidate"]["resources"]["known_totals"]["external_credit_micros"], 8)
        self.assertIsNone(report["comparisons"][0]["resource_reduction_percent"])

    def test_mismatched_budget_validator_or_provenance_is_not_comparable(self):
        report = run_benchmark(**self.fixture())
        baseline, candidate = report["profiles"].values()
        for key, value in (("budget", {"external_credit_micros": 999}),
                           ("validator_id", "other"), ("source_id", "other"),
                           ("provenance", "measured"), ("cohort_sha256", "other")):
            with self.subTest(key=key):
                changed = deepcopy(candidate)
                changed[key] = value
                self.assertFalse(compare_profiles(baseline, changed)["comparable"])

    def test_validator_errors_fail_closed_and_no_truthy_nonboolean(self):
        for validator in (lambda task, output: 1,
                          lambda task, output: (_ for _ in ()).throw(RuntimeError("secret"))):
            options = self.fixture()
            options["validator"] = validator
            report = run_benchmark(**options)["profiles"]["base"]
            self.assertEqual(report["counts"]["verified_successes"], 0)
            self.assertEqual(report["counts"]["validator_errors"], 2)
            self.assertNotIn("secret", str(report))

    def test_validator_cannot_mutate_other_profiles(self):
        options = self.fixture()
        original = deepcopy(options["tasks"])
        def validator(task, output):
            expected = task["expected"]
            task["expected"] = -1
            return output == expected
        options["validator"] = validator
        report = run_benchmark(**options)
        self.assertEqual(options["tasks"], original)
        self.assertEqual(report["profiles"]["candidate"]["counts"]["verified_successes"], 2)

    def test_bad_traces_rejected_before_validator_runs(self):
        mutations = [
            lambda x: x["profiles"]["candidate"].pop(),
            lambda x: x["profiles"]["candidate"][1].update(task_id="a"),
            lambda x: x["profiles"]["candidate"][0].update(claimed_success=1),
            lambda x: x["profiles"]["candidate"][0]["attempts"][0]["resources"].update(input_tokens=-1),
            lambda x: x["profiles"]["candidate"][0]["attempts"][0]["resources"].update(input_tokens=True),
            lambda x: x["profiles"]["candidate"][0]["attempts"][0]["resources"].update(input_tokens=float("nan")),
            lambda x: x["profiles"]["candidate"][0]["attempts"][0]["resources"].update(cached_input_tokens=11),
            lambda x: x["profiles"]["candidate"][1]["attempts"][0].update(attempt_id="a"),
            lambda x: x["profiles"]["candidate"][0]["attempts"][0].pop("latency_ms"),
            lambda x: x["profiles"]["candidate"][0].update(attempts=[]),
            lambda x: x["budget"].update(external_credit_micros=True),
            lambda x: x.update(provenance="trusted"),
            lambda x: x.update(validator_id=None),
        ]
        for mutate in mutations:
            options, calls = self.fixture(), []
            options["validator"] = lambda task, output: calls.append(task) or True
            mutate(options)
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError):
                    run_benchmark(**options)
                self.assertEqual(calls, [])

    def test_comparison_rejects_malformed_or_nonconserving_reports(self):
        profiles = run_benchmark(**self.fixture())["profiles"]
        for mutate in (lambda x: x.pop("tasks"),
                       lambda x: x["counts"].update(verified_successes=100),
                       lambda x: x["resources"]["known_totals"].update(input_tokens=0),
                       lambda x: x.update(budget_status="EXCEEDED")):
            report = deepcopy(profiles["candidate"])
            mutate(report)
            with self.assertRaises(ValueError):
                compare_profiles(profiles["base"], report)

    def test_measured_is_declared_and_not_a_billing_authentication(self):
        options = self.fixture()
        options["provenance"] = "measured"
        report = run_benchmark(**options)
        self.assertIn("not independently authenticated", report["measurement_trust"])
        self.assertFalse(report["production_savings_proven"])


if __name__ == "__main__":
    unittest.main()
