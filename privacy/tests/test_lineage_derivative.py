"""Regression: lineage completeness and derivative-data risk analysis."""

import os
import sys
import unittest

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy.lineage import (
    LineageGraph, LineageRecord, LineageInput, LineageTransform,
)
from keel.privacy.derivative_analysis import analyze_derivative


class TestLineage(unittest.TestCase):
    def test_complete_chain_verifies(self):
        g = LineageGraph()
        g.add(LineageRecord(
            artifact_id="artifact-001",
            inputs=[LineageInput(input_id="src.csv", input_digest="d" * 64,
                                 input_kind="file")],
            transforms=[LineageTransform(step=1, name="dedup",
                                         description="dedup by id",
                                         code_ref="engines/dedup.py")],
            produced_by="test",
        ))
        complete, gaps = g.verify_chain("artifact-001")
        self.assertTrue(complete)
        self.assertEqual(gaps, [])

    def test_unknown_provenance_is_a_gap(self):
        g = LineageGraph()
        g.add(LineageRecord(
            artifact_id="artifact-001",
            inputs=[LineageInput(input_id="mystery", input_kind="unknown")],
        ))
        complete, gaps = g.verify_chain("artifact-001")
        self.assertFalse(complete)
        self.assertTrue(any("UNKNOWN" in x for x in gaps))

    def test_missing_record_is_a_gap(self):
        g = LineageGraph()
        complete, gaps = g.verify_chain("artifact-999")
        self.assertFalse(complete)

    def test_gaps_never_silently_pass(self):
        # An empty record must not verify: origin unknown is a defect.
        g = LineageGraph()
        g.add(LineageRecord(artifact_id="artifact-001"))
        complete, _ = g.verify_chain("artifact-001")
        self.assertFalse(complete)


class TestDerivativeAnalysis(unittest.TestCase):
    def test_unbounded_contribution_flagged(self):
        a = analyze_derivative(
            "artifact-001",
            methodology={"function": "count", "grouping_keys": ["employer"]},
            contribution_bounds={},  # unbounded
            bucket_sizes=[10, 20, 30],
            release_series_context="single release",
        )
        self.assertEqual(a.verdict, "NEEDS_MITIGATION")
        self.assertTrue(any("UNBOUNDED" in x for x in a.findings))
        self.assertTrue(a.open_questions)

    def test_small_cells_flagged(self):
        a = analyze_derivative(
            "artifact-001",
            methodology={"function": "mean", "grouping_keys": ["region"]},
            contribution_bounds={"max_per_unit": 1, "bound_method": "dedup"},
            bucket_sizes=[100, 3, 50],  # one small cell
            suppression_threshold=5,
            release_series_context="single release",
        )
        self.assertEqual(a.verdict, "NEEDS_MITIGATION")
        self.assertTrue(any("n < suppression" in x for x in a.findings))

    def test_missing_methodology_flagged(self):
        a = analyze_derivative("artifact-001", methodology={},
                               contribution_bounds={"max_per_unit": 1,
                                                    "bound_method": "cap"},
                               bucket_sizes=[10])
        self.assertTrue(any("unstated" in x for x in a.findings))

    def test_missing_series_context_flagged(self):
        a = analyze_derivative(
            "artifact-001",
            methodology={"function": "count", "grouping_keys": ["k"]},
            contribution_bounds={"max_per_unit": 1, "bound_method": "dedup"},
            bucket_sizes=[10, 20])
        self.assertTrue(any("differencing" in x for x in a.findings))

    def test_clean_analysis_still_requires_review(self):
        a = analyze_derivative(
            "artifact-001",
            methodology={"function": "count", "grouping_keys": ["k"]},
            contribution_bounds={"max_per_unit": 1, "bound_method": "dedup"},
            bucket_sizes=[10, 20],
            release_series_context="single release",
        )
        self.assertEqual(a.verdict, "NO_AUTOMATED_FINDINGS")
        # The module never clears for release on its own.
        self.assertTrue(any("counsel review" in x for x in a.open_questions))

    def test_serializes(self):
        a = analyze_derivative("a1", {"function": "count"},
                               {"max_per_unit": 1})
        d = a.to_dict()
        self.assertEqual(d["artifact_id"], "a1")
        self.assertIn("verdict", d)


if __name__ == "__main__":
    unittest.main()
