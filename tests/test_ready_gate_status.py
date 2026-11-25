"""Offline READY status validation; synthetic records and no workspace reads."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import apply_loop
import ready_gate


def lead(**changes):
    return {"role_id": "synthetic-role", "company": "Example",
            "title": "Synthetic role", "status": "READY",
            "action_band": "APPLY", "fit_score": 75, **changes}


class ReadyStatusTests(unittest.TestCase):
    def test_non_string_status_is_blocked_without_mutation(self):
        for status in ([], ["READY"], {}, {"state": "READY"}, None, True, 0, 75):
            with self.subTest(status=status):
                entry = lead(status=status)
                before = deepcopy(entry)
                result = ready_gate.entry_admission(entry)
                self.assertFalse(result["allowed"])
                self.assertIn("not_ready", result["reason_codes"])
                self.assertEqual(entry, before)

    def test_eligibility_rejects_containers_before_downstream_checks(self):
        for status in ([], ["READY"], {}, {"state": "READY"}):
            with self.subTest(status=status):
                with patch.object(apply_loop, "_tripwire_skip",
                                  side_effect=AssertionError("downstream check reached")) as downstream:
                    self.assertEqual(apply_loop.eligible(lead(status=status)),
                                     (False, "not READY"))
                    downstream.assert_not_called()

    def test_bad_status_does_not_interrupt_following_admission(self):
        entries = [lead(status={}), lead(role_id="synthetic-next")]
        self.assertEqual([ready_gate.entry_admission(e)["allowed"] for e in entries],
                         [False, True])

    def test_known_ready_states_keep_other_gates(self):
        for status in ("READY", "READY-FOR-BROWSER"):
            with self.subTest(status=status):
                self.assertTrue(ready_gate.entry_admission(lead(status=status))["allowed"])
                held = ready_gate.entry_admission(lead(status=status, human_hold=True))
                self.assertFalse(held["allowed"])
                self.assertIn("explicit_hold:human_hold", held["reason_codes"])
                self.assertFalse(ready_gate.entry_admission(
                    lead(status=status, fit_score=74))["allowed"])

    def test_unrecognized_string_status_remains_blocked(self):
        for status in ("", "ready", "SUBMITTED", "BLOCKED-PENDING-INPUT"):
            with self.subTest(status=status):
                self.assertFalse(ready_gate.entry_admission(lead(status=status))["allowed"])

    def test_preparation_mode_keeps_holds_without_ready_requirement(self):
        entry = lead(status={})
        self.assertTrue(ready_gate.entry_admission(entry, require_ready=False)["allowed"])
        entry["human_hold"] = True
        held = ready_gate.entry_admission(entry, require_ready=False)
        self.assertFalse(held["allowed"])
        self.assertIn("explicit_hold:human_hold", held["reason_codes"])


if __name__ == "__main__":
    unittest.main()
