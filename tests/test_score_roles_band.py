"""Tests for score_roles.py -- fit-score band boundaries.

Covers the band() thresholds (>=90 PRIORITY, >=82 APPLY, >=72 STRATEGIC,
else SKIP) against engines/fit-scoring-model.md.

Run: python3 test_score_roles_band.py
"""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import score_roles

BAND_CASES = [
    (100, "PRIORITY"),
    (90, "PRIORITY"),
    (89, "APPLY"),
    (82, "APPLY"),
    (81, "STRATEGIC"),
    (72, "STRATEGIC"),
    (71, "SKIP"),
    (0, "SKIP"),
]


class TestBand(unittest.TestCase):
    def test_boundaries_and_extremes(self):
        for score, expected in BAND_CASES:
            with self.subTest(score=score):
                self.assertEqual(score_roles.band(score), expected)

    def test_band_names_match_rubric(self):
        rubric_bands = {"PRIORITY", "APPLY", "STRATEGIC", "SKIP"}
        observed = {score_roles.band(s) for s in range(0, 101)}
        self.assertEqual(observed, rubric_bands)


if __name__ == "__main__":
    unittest.main(verbosity=2)
