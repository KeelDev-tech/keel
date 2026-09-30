"""Question-resolution policy uses the canonical intake authority."""
import os
import unittest
from unittest import mock

from engines import qresolve_policy as policy


class QresolvePolicyTests(unittest.TestCase):
    def test_default_follows_canonical_floor_and_ignores_tray_override(self):
        with mock.patch.dict(os.environ, {"KEEL_TRAY_MIN_FIT": "60"}), \
                mock.patch.object(policy.fit_policy, "main_floor", return_value=83):
            self.assertEqual(policy.validate_min_fit(), 83)
            self.assertFalse(policy.fit_admission({"fit_score": 82})["eligible"])
            self.assertTrue(policy.fit_admission({"fit_score": 83})["eligible"])

    def test_explicit_floor_can_only_tighten_current_canonical_policy(self):
        with mock.patch.object(policy.fit_policy, "main_floor", return_value=75):
            self.assertEqual(policy.validate_min_fit(85), 85)
            for value in (74, True, "nan", "inf", 101, None):
                with self.subTest(value=value):
                    if value is None:
                        self.assertEqual(policy.validate_min_fit(value), 75)
                    else:
                        with self.assertRaises(ValueError):
                            policy.validate_min_fit(value)

    def test_missing_and_invalid_scores_fail_closed(self):
        self.assertEqual(policy.fit_admission({})["reason"], "missing_fit")
        for value in (True, False, [], {}, "75", "nan", "inf", "", -1, 101):
            with self.subTest(value=value):
                result = policy.fit_admission({"fit_score": value})
                self.assertFalse(result["eligible"])
                self.assertEqual(result["reason"], "invalid_fit")
        self.assertTrue(policy.fit_admission({"fit_score": 75})["eligible"])

    def test_identity_changes_for_posting_presence_but_not_queue_state(self):
        row = {"role_id": "one", "employer": "Co", "title": "Role",
               "posting_url": "https://example.com/job"}
        digest = policy.identity_digest(row)
        self.assertEqual(digest, policy.identity_digest({**row, "status": "READY", "fit_score": 80}))
        self.assertNotEqual(digest, policy.identity_digest({**row, "posting_url": "https://example.com/new"}))
        self.assertNotEqual(digest, policy.identity_digest({**row, "job_id": None}))
        self.assertNotEqual(digest, policy.identity_digest({**row, "role_id": "two"}))
        self.assertNotEqual(digest, policy.identity_digest({**row, "role_title": "Other Role"}))


if __name__ == "__main__":
    unittest.main()
