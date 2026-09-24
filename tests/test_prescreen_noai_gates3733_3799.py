#!/usr/bin/env python3
"""Regression tests: prescreen no-AI attestation leak, gates 3733/3799 repair
(2026-09-21).

THE DEFECT: the prescreen attestation matcher auto-agreed combined
attestation text as CLEAN:
  - "I certify the information is true AND I worked without assistance"
    -> CLEAN (the certify-family detector hole: "certify" matches BANK_MAP's
    truthfulness rule but never fires ATTEST_RE, and checkbox statements
    never reach the required-text route).
  - "I attest that all information is true and I did not use any AI tools"
    -> CLEAN (no-AI vocabulary hole + window-only screening).
This violates the standing rule: no-AI / unaided-work /
personally-completed attestations ALWAYS park (auto-agreeing them would be
FALSE).

THE REPAIR (keel/engines/prescreen.py):
  - NO_AI_ATTEST_RE widened (no AI spaced, without automated assistance,
    without assistance, independently, did-not/have-not-used-AI frame;
    legacy matches kept, never narrowed).
  - _no_ai_hard_stop() screens the FULL question text (form-intel line +
    window) BEFORE every preauthorized mapping path, and inside
    question_mappable() itself (covers the required-text route).
  - No-AI attestation-statement sweep: any checkbox/radio/dropdown line
    carrying a no-AI clause parks (lines the ATTEST_RE loop already
    adjudicated are never double-parked).
  - Employer scope (confirmed blocker patterns) binds BEFORE any
    answer-bank reuse.
  - Pre-authorized scope unchanged: arbitration, background-check consent,
    at-will, information-truthfulness ALONE, data-privacy consent still
    auto-handle.

Hermetic: synthetic bank only, never the operator's live answer_bank.json.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engines"))
import prescreen


BANK = {"answers": {
    "arbitration_agreement": "Yes",
    "background_check_consent": "Yes",
    "at_will_acknowledgment": "Yes",
    "information_truthfulness_attestation": "Yes",
    "data_privacy_consent": "Yes",
    "us_work_auth": "Yes",
}}


def make_packet(company, lines, role_id="TEST-1"):
    intel = "\n".join(lines)
    brief = (
        "Submit a job application for the applicant.\n"
        "GATES (stop conditions -- obey exactly):\n"
        "  - travel_attestation: STOP and ask the applicant.\n"
        "FORM INTEL \u2014 VERIFIED PRE-LAUNCH (do not re-derive; use these exact labels):\n"
        f"{intel}\n"
        "STEP 3 \u2014 COMMIT TECHNIQUE FOR GREENHOUSE: live-option click + per-field verify\n"
    )
    return {"role_id": role_id, "company": company, "title": "Test Role",
            "brief": brief}


class TestNoAiHardStop(unittest.TestCase):
    def test_widened_vocabulary(self):
        yes = [
            "I worked without assistance",
            "I attest the above is true and I used no AI",
            "I used no-AI tools",  # hyphenated still matches
            "completed without automated assistance",
            "I worked independently on this application",
            "I did not use any AI tools",
            "I have not used artificial intelligence",
            "I personally completed this application",
            "I worked unaided",
        ]
        for t in yes:
            self.assertTrue(prescreen._no_ai_hard_stop(t), f"missed: {t}")

    def test_ai_experience_questions_not_matched(self):
        no = [
            "Have you used AI tools in your work?",
            "Describe your experience with AI-assisted development",
            "What is your name?",
            "Do you certify you are authorized to work in the US?",
        ]
        for t in no:
            self.assertFalse(prescreen._no_ai_hard_stop(t), f"false hit: {t}")


class TestCombinedAttestationsPark(unittest.TestCase):
    def test_certify_plus_without_assistance_checkbox_parks(self):
        # The named detector hole: "certify" never fires ATTEST_RE and
        # checkbox statements never reach the required-text route.
        p = make_packet("TestCo", [
            "- [checkbox] I certify the information I have provided is true "
            "and complete and I worked without assistance",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")
        self.assertTrue(any("attest" in r for r in res["reasons"]))

    def test_attest_plus_did_not_use_ai_checkbox_parks(self):
        p = make_packet("TestCo", [
            "- [checkbox] I attest that all information I have provided is "
            "true and I did not use any AI tools",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")
        self.assertTrue(any("attest" in r for r in res["reasons"]))

    def test_truthfulness_plus_independently_checkbox_parks(self):
        p = make_packet("TestCo", [
            "- [checkbox] I certify that the information above is true and "
            "that I completed this assessment independently",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")

    def test_bare_without_assistance_checkbox_parks(self):
        # No certify/attest token at all -- still a no-AI attestation.
        p = make_packet("TestCo", [
            "- [checkbox] I confirm that I worked without assistance",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")

    def test_text_plus_personally_completed_parks(self):
        p = make_packet("TestCo", [
            "- [text] I certify that all information provided is true and "
            "that I personally completed this application*",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")
        self.assertTrue(any("attest" in r for r in res["reasons"]))

    def test_noai_clause_far_from_attest_token_parks(self):
        # The truthfulness clause sits fully inside the +-250-char window
        # (so pre-fix it auto-cleared as preauthorized) while the no-AI
        # clause sits ~400 chars away -- full-line screening must catch it.
        filler = " and ".join(["the foregoing statements"] * 16)
        p = make_packet("TestCo", [
            "- [checkbox] I attest that the information provided is true "
            f"and complete and {filler} and I worked without assistance",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")
        self.assertTrue(any("attest" in r for r in res["reasons"]))


class TestPreauthorizedScopeUnchanged(unittest.TestCase):
    def _clean(self, lines):
        p = make_packet("TestCo", lines)
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "CLEAN", f"reasons: {res['reasons']}")
        self.assertEqual(res["reasons"], [])

    def test_truthfulness_alone_stays_clean(self):
        self._clean(["- [checkbox] I attest that the information I have "
                     "provided is true and complete"])

    def test_arbitration_alone_stays_clean(self):
        self._clean(["- [checkbox] I have read and agree to the arbitration "
                     "agreement"])

    def test_background_check_alone_stays_clean(self):
        self._clean(["- [checkbox] I consent to a background check and "
                     "consumer report"])

    def test_at_will_alone_stays_clean(self):
        self._clean(["- [checkbox] I acknowledge this is an at-will "
                     "employment relationship"])

    def test_data_privacy_alone_stays_clean(self):
        self._clean(["- [checkbox] I consent to data processing per the "
                     "privacy policy"])

    def test_certify_work_auth_alone_stays_clean(self):
        # "certify" alone must NOT false-park: a work-auth question with no
        # no-AI clause maps to the banked answer and stays CLEAN.
        self._clean(["- [text] Do you certify you are authorized to work in "
                     "the US?*"])


class TestEmployerScopeBeforeReuse(unittest.TestCase):
    def test_employer_blocker_parks_despite_preauthorized_mapping(self):
        # Employer scope binds BEFORE answer-bank reuse: the attestation
        # would auto-clear as preauthorized, but the employer's confirmed
        # blocker pattern still parks.
        p = make_packet("Acme", [
            "- [checkbox] I have read and agree to the arbitration agreement",
        ])
        patterns = {"acme": ["arbitration agreement"]}
        res = prescreen.screen_packet(p, BANK, patterns)
        self.assertEqual(res["verdict"], "PARK", f"reasons: {res['reasons']}")
        self.assertTrue(any("Employer form pattern" in r for r in res["reasons"]))


if __name__ == "__main__":
    unittest.main()
