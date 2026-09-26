import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.actions.classifier import ActionClass
from security.policy.risk import risk_score


class TestRisk(unittest.TestCase):
    def test_deterministic(self):
        s1, f1 = risk_score(ActionClass.SUBMISSION, "INTERNAL", None, False)
        s2, f2 = risk_score(ActionClass.SUBMISSION, "INTERNAL", None, False)
        self.assertEqual((s1, f1), (s2, f2))

    def test_ordering(self):
        ro, _ = risk_score(ActionClass.READ_ONLY)
        sub, _ = risk_score(ActionClass.SUBMISSION)
        des, _ = risk_score(ActionClass.DESTRUCTIVE)
        self.assertLess(ro, sub)
        self.assertLess(sub, des)

    def test_injection_raises(self):
        clean, _ = risk_score(ActionClass.SUBMISSION, "INTERNAL", None)
        hot, _ = risk_score(ActionClass.SUBMISSION, "INTERNAL", "HIGH")
        self.assertGreater(hot, clean)

    def test_sensitivity_raises(self):
        a, _ = risk_score(ActionClass.READ_ONLY, "PUBLIC")
        b, _ = risk_score(ActionClass.READ_ONLY, "CREDENTIAL")
        self.assertGreater(b, a)

    def test_capped_at_100(self):
        s, _ = risk_score(ActionClass.DESTRUCTIVE, "CREDENTIAL", "HIGH",
                          True)
        self.assertLessEqual(s, 100)

    def test_delegation_hop_adds(self):
        a, _ = risk_score(ActionClass.READ_ONLY, "INTERNAL", None, False)
        b, _ = risk_score(ActionClass.READ_ONLY, "INTERNAL", None, True)
        self.assertEqual(b, a + 10)


if __name__ == "__main__":
    unittest.main()
