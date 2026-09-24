"""Regression tests for the deterministic promotion gates (Workstream G).

unittest-style so they run under plain stdlib (python3 -m unittest) as well
as pytest. No I/O, no network, no LLM.
"""

import unittest

from promotion.gates import (
    INVARIANTS,
    STAGES,
    STAGE_MIN_CASES,
    VERDICT_BLOCK,
    VERDICT_PROMOTE,
    evaluate,
    evidence_digest,
    evidence_schema,
)


def valid_report(stage):
    """A complete, clean evidence report for the given stage."""
    idx = STAGES.index(stage)
    report = {
        "stage": stage,
        "invariants": {name: 0 for name in INVARIANTS},
        "case_count": STAGE_MIN_CASES[stage],
        "authentic_source_count": 1,
        "genuine_decision_count": 1,
    }
    if idx == 0:
        report["prior_stage_name"] = None
        report["prior_stage_digest"] = None
    else:
        report["prior_stage_name"] = STAGES[idx - 1]
        report["prior_stage_digest"] = "sha256:" + "ab" * 32
    return report


class TestPromoteOnCleanEvidence(unittest.TestCase):
    def test_all_zero_complete_evidence_promotes_every_stage(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                result = evaluate(valid_report(stage))
                self.assertEqual(result["verdict"], VERDICT_PROMOTE)
                self.assertEqual(result["reasons"], [])

    def test_result_shape(self):
        result = evaluate(valid_report("canary"))
        self.assertEqual(set(result.keys()), {"verdict", "reasons"})
        self.assertIsInstance(result["reasons"], list)

    def test_canary_without_prior_keys_also_promotes(self):
        report = valid_report("canary")
        del report["prior_stage_name"]
        del report["prior_stage_digest"]
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_PROMOTE)

    def test_deterministic(self):
        first = evaluate(valid_report("stage50"))
        second = evaluate(valid_report("stage50"))
        self.assertEqual(first, second)

    def test_extra_case_count_above_minimum_promotes(self):
        report = valid_report("stage10")
        report["case_count"] = 10_000
        self.assertEqual(evaluate(report)["verdict"], VERDICT_PROMOTE)


class TestZeroToleranceInvariants(unittest.TestCase):
    def test_each_invariant_nonzero_in_isolation_blocks(self):
        for name in INVARIANTS:
            with self.subTest(invariant=name):
                report = valid_report("hetero5")
                report["invariants"][name] = 1
                result = evaluate(report)
                self.assertEqual(result["verdict"], VERDICT_BLOCK)
                self.assertTrue(
                    any(name in reason for reason in result["reasons"]),
                    "invariant %r must be named in reasons" % name,
                )

    def test_large_count_blocks(self):
        report = valid_report("stage100")
        report["invariants"]["wrong_target_execution"] = 999
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        self.assertTrue(
            any("wrong_target_execution" in r for r in result["reasons"])
        )

    def test_multiple_violations_all_named(self):
        report = valid_report("fleet")
        report["invariants"]["synthetic_to_live"] = 2
        report["invariants"]["unauthorized_egress"] = 1
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        joined = "\n".join(result["reasons"])
        self.assertIn("synthetic_to_live", joined)
        self.assertIn("unauthorized_egress", joined)


class TestNoStageSkipping(unittest.TestCase):
    def test_skip_one_stage_rejected(self):
        report = valid_report("stage50")
        report["prior_stage_name"] = "hetero5"  # must be stage10
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        self.assertTrue(
            any("stage skipping rejected" in r for r in result["reasons"])
        )

    def test_skip_to_fleet_rejected(self):
        report = valid_report("fleet")
        report["prior_stage_name"] = "canary"  # must be stage100
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_second_stage_with_no_prior_rejected(self):
        report = valid_report("hetero5")
        report["prior_stage_name"] = None
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_canary_with_prior_stage_rejected(self):
        report = valid_report("canary")
        report["prior_stage_name"] = "canary"
        report["prior_stage_digest"] = "abc"
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_missing_prior_digest_rejected(self):
        report = valid_report("stage10")
        del report["prior_stage_digest"]
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_empty_prior_digest_rejected(self):
        report = valid_report("stage10")
        report["prior_stage_digest"] = ""
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)


class TestFailClosed(unittest.TestCase):
    def test_unknown_top_level_field_blocks(self):
        report = valid_report("canary")
        report["reviewer_notes"] = "looks fine"
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        self.assertTrue(
            any("unknown evidence field" in r for r in result["reasons"])
        )

    def test_unknown_invariant_key_blocks(self):
        report = valid_report("stage10")
        report["invariants"]["fabricated_provenance_typo"] = 0
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        self.assertTrue(any("unknown invariant" in r for r in result["reasons"]))

    def test_unknown_stage_blocks(self):
        report = valid_report("canary")
        report["stage"] = "supersonic"
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        self.assertTrue(any("unknown stage" in r for r in result["reasons"]))

    def test_non_dict_report_blocks(self):
        for bad in (None, [], "canary", 42):
            with self.subTest(bad=bad):
                result = evaluate(bad)
                self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_missing_stage_blocks(self):
        report = valid_report("canary")
        del report["stage"]
        self.assertEqual(evaluate(report)["verdict"], VERDICT_BLOCK)

    def test_missing_invariants_blocks(self):
        report = valid_report("canary")
        del report["invariants"]
        self.assertEqual(evaluate(report)["verdict"], VERDICT_BLOCK)

    def test_missing_single_invariant_count_blocks(self):
        report = valid_report("canary")
        del report["invariants"]["unauthorized_egress"]
        self.assertEqual(evaluate(report)["verdict"], VERDICT_BLOCK)

    def test_negative_count_blocks(self):
        report = valid_report("canary")
        report["invariants"]["false_human_approval"] = -1
        self.assertEqual(evaluate(report)["verdict"], VERDICT_BLOCK)

    def test_bool_count_rejected(self):
        report = valid_report("canary")
        report["invariants"]["fabricated_provenance"] = False
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_float_count_rejected(self):
        report = valid_report("canary")
        report["invariants"]["fabricated_provenance"] = 0.0
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_string_count_rejected(self):
        report = valid_report("canary")
        report["case_count"] = "5"
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_invariants_not_mapping_blocks(self):
        report = valid_report("canary")
        report["invariants"] = []
        self.assertEqual(evaluate(report)["verdict"], VERDICT_BLOCK)

    def test_case_count_below_stage_minimum_blocks(self):
        report = valid_report("canary")
        report["case_count"] = 0
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)
        self.assertTrue(
            any("insufficient evidence" in r for r in result["reasons"])
        )

    def test_zero_authentic_sources_blocks(self):
        report = valid_report("hetero5")
        report["authentic_source_count"] = 0
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_zero_genuine_decisions_blocks(self):
        report = valid_report("hetero5")
        report["genuine_decision_count"] = 0
        result = evaluate(report)
        self.assertEqual(result["verdict"], VERDICT_BLOCK)

    def test_missing_case_count_blocks(self):
        report = valid_report("canary")
        del report["case_count"]
        self.assertEqual(evaluate(report)["verdict"], VERDICT_BLOCK)


class TestDigestHelper(unittest.TestCase):
    def test_digest_deterministic_and_hex(self):
        first = evidence_digest(b'{"stage":"canary"}')
        second = evidence_digest(b'{"stage":"canary"}')
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)  # valid hex

    def test_digest_changes_with_input(self):
        self.assertNotEqual(evidence_digest(b"a"), evidence_digest(b"b"))

    def test_digest_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            evidence_digest('{"stage":"canary"}')


class TestSchema(unittest.TestCase):
    def test_schema_lists_all_stages_and_invariants(self):
        schema = evidence_schema()
        self.assertEqual(schema["stages"], list(STAGES))
        self.assertEqual(schema["invariants"], list(INVARIANTS))
        self.assertTrue(schema["fail_closed"])
        for stage in STAGES:
            self.assertIn(stage, schema["stage_min_cases"])


if __name__ == "__main__":
    unittest.main()
