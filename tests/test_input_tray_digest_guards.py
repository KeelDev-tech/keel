"""test_input_tray_digest_guards.py — tray digest 2026-09-22 safety ports.

Covers:
  - draft_for scope veto: an employer-scoped bank entry ("employer:X") is
    never suggested on a question/employer outside its scope.
  - draft_for employer veto: multi-employer cards must not carry an
    employer-scoped draft (scope veto handles it via employer=None).
  - POLICY_DECIDED_PAT catches "100% onsite" wordings (2026-09-22 add).
  - RETIRED_CARD_KEYS lists the retired/quarantined cards (2026-09-22
    standing directives).
  - load_bank skips draftable=False reference entries and prefers
    'value' over legacy 'answer'.

unittest style, run with:
    python3 -m unittest tests.test_input_tray_digest_guards
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))
import input_tray_digest as itd  # noqa: E402


class TestScopeVeto(unittest.TestCase):
    def setUp(self):
        # (key, human value, scope)
        self.banked = [
            ("interview_recording_consent_acmecorp", "Yes, AcmeCorp only",
             "employer:AcmeCorp"),
        ]

    def test_in_scope_employer_suggests(self):
        d = itd.draft_for("interview recording consent for AcmeCorp review",
                          self.banked, employer="AcmeCorp")
        self.assertIsNotNone(d)
        self.assertEqual(d["bank_key"], "interview_recording_consent_acmecorp")

    def test_out_of_scope_employer_silence(self):
        d = itd.draft_for("interview recording consent for review",
                          self.banked, employer="OtherCorp")
        self.assertIsNone(d)

    def test_multi_employer_card_vetoed(self):
        # collect_cards passes employer=None for multi-employer cards;
        # every employer-scoped draft is then vetoed.
        d = itd.draft_for("interview recording consent for review",
                          self.banked, employer=None)
        self.assertIsNone(d)

    def test_scope_named_in_question_allows(self):
        d = itd.draft_for("interview recording consent for AcmeCorp interview",
                          self.banked, employer="OtherCorp")
        self.assertIsNotNone(d)

    def test_global_entry_still_suggests(self):
        banked = [("work_auth_general", "Yes", "")]
        d = itd.draft_for("work auth authorization status", banked,
                          employer="OtherCorp")
        self.assertIsNotNone(d)


class TestEmployerTailVeto(unittest.TestCase):
    def test_tail_key_vetoed_on_mismatched_question(self):
        banked = [("prior_employment_acmecorp", "2020-2023", "")]
        d = itd.draft_for("prior employment history dates",
                          banked, employer="OtherCorp")
        # tail token 'acmecorp' not named in the question -> vetoed
        self.assertIsNone(d)

    def test_generic_tail_never_vetoed(self):
        banked = [("travel_willingness_policy", "Up to 25%", "")]
        d = itd.draft_for("travel willingness policy question",
                          banked, employer="OtherCorp")
        self.assertIsNotNone(d)


class TestPolicyDecidedPattern(unittest.TestCase):
    def test_100_percent_onsite_matches(self):
        self.assertIsNotNone(
            itd.POLICY_DECIDED_PAT.search(
                "Are you able to work 100% onsite in Seattle?"))

    def test_full_time_onsite_still_matches(self):
        self.assertIsNotNone(
            itd.POLICY_DECIDED_PAT.search("This is a full-time on-site role."))

    def test_unrelated_not_matched(self):
        self.assertIsNone(
            itd.POLICY_DECIDED_PAT.search("Are you authorized to work in the US?"))


class TestRetiredCardKeys(unittest.TestCase):
    def test_expected_keys_present(self):
        self.assertIn("d94b6c7b9c8207c0", itd.RETIRED_CARD_KEYS)
        self.assertIn("21e7331771acdadd", itd.RETIRED_CARD_KEYS)
        self.assertIn("7a642d5375865492", itd.RETIRED_CARD_KEYS)

    def test_frozenset_type(self):
        self.assertIsInstance(itd.RETIRED_CARD_KEYS, frozenset)


class TestLoadBank(unittest.TestCase):
    def test_draftable_false_skipped_and_value_preferred(self):
        real_load = itd.load

        def fake_load(path, default=None):
            return {"answers": {
                "policy_ref": {"answer": "reference only", "draftable": False,
                               "value": "reference only", "scope": ""},
                "work_auth_us": {"answer": "legacy", "value": "Yes",
                                 "scope": ""},
                "plain_key": "plain value",
            }}

        itd.load = fake_load
        try:
            banked = itd.load_bank()
        finally:
            itd.load = real_load
        keys = [k for k, v, s in banked]
        self.assertNotIn("policy_ref", keys)
        by_key = {k: (v, s) for k, v, s in banked}
        self.assertEqual(by_key["work_auth_us"][0], "Yes")
        self.assertEqual(by_key["plain_key"][0], "plain value")


if __name__ == "__main__":
    unittest.main()
