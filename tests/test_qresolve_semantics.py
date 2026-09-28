"""Synthetic routing fixtures: not qualification on the private real-card corpus."""

import json
import subprocess
import sys
import unittest

from engines.qresolve_semantics import (
    FACT_QUESTION_ALIASES, canonical_fingerprint, classify, fact_key,
)


# All fixtures are synthetic and contain no real applicants or employers. These
# test policy boundaries; the private >=50-real-card acceptance gate remains a
# host-side qualification task, not an accuracy claim made by this test set.
SYNTHETIC_LABELED_CARDS = [
    ("FACT", {"question": question}, [])
    for aliases in FACT_QUESTION_ALIASES.values() for question in aliases[:3]
] + [
    ("STRUCTURAL", {"question": "Application route is apply-by-email"}, [{
        "employer": "Example Employer A", "unresolved": ["Application route is apply-by-email"],
        "queue_notes": "Prior bank sweep retracted: an email fact cannot authorize the route.",
    }]),
    ("STRUCTURAL", {"question": "Email address"}, [{"status_reason": "Apply-by-email route block"}]),
    ("STRUCTURAL", {"question": "Email address", "source": "intel_gap"}, []),
    ("STRUCTURAL", {"question": "First name", "source": "software_error"}, []),
    ("STRUCTURAL", {"question": "Last name", "source": "stale_packet"}, []),
    ("STRUCTURAL", {"question": "hCaptcha takeover"}, []),
    ("STRUCTURAL", {"question": "DataDome challenge"}, []),
    ("STRUCTURAL", {"question": "Create an account"}, []),
    ("STRUCTURAL", {"question": "Email verification code screen"}, []),
    ("STRUCTURAL", {"question": "Submit button unresponsive"}, []),
    ("STRUCTURAL", {"question": "Form unreadable"}, []),
    ("STRUCTURAL", {"question": "First name", "status": "SYSTEM-BLOCKED"}, []),
    ("STRUCTURAL", {"question": "Office policy hard block"}, []),
    ("STRUCTURAL", {"question": "Browser session approval denied at platform level"}, []),
    ("JUDGMENT", {"question": "What is your availability?"}, [{
        "employer": "Example Employer B", "queue_notes": "Schedule selection remains the applicant's decision.",
    }]),
    ("JUDGMENT", {"question": "Are you willing to relocate?"}, []),
    ("JUDGMENT", {"question": "Can you travel 25%?"}, []),
    ("JUDGMENT", {"question": "Can you work in the office 3 days each week?"}, []),
    ("JUDGMENT", {"question": "When can you start?"}, []),
    ("JUDGMENT", {"question": "Select a workstream"}, []),
    ("JUDGMENT", {"question": "Proceed with this held lead?"}, []),
    ("JUDGMENT", {"question": "Drop this lead?"}, []),
    ("JUDGMENT", {"question": "Desired salary"}, []),
    ("JUDGMENT", {"question": "Can you work weekends?"}, []),
    ("TRENT-ONLY", {"question": "Do you consent to interview recording?"}, [{
        "employer": "Example Employer C", "queue_notes": "Quarantined employer-specific answer must not propagate.",
    }]),
    ("TRENT-ONLY", {"question": "I certify this was personally completed without AI."}, []),
    ("TRENT-ONLY", {"question": "No-AI attestation"}, []),
    ("TRENT-ONLY", {"question": "Confirm your work was unaided."}, []),
    ("TRENT-ONLY", {"question": "Why are you interested in this role?"}, []),
    ("TRENT-ONLY", {"question": "Please provide a personal statement."}, []),
    ("TRENT-ONLY", {"question": "What exceptional work have you done?"}, []),
    ("TRENT-ONLY", {"question": "Describe your greatest achievement."}, []),
    ("TRENT-ONLY", {"question": "Do you agree to the privacy policy?"}, []),
    ("TRENT-ONLY", {"question": "Are you authorized to work in this country?"}, []),
    ("TRENT-ONLY", {"question": "Do you need sponsorship?"}, []),
    ("TRENT-ONLY", {"question": "Your references"}, []),
    ("TRENT-ONLY", {"question": "What medical conditions do you have?"}, []),
    ("TRENT-ONLY", {"question": "Your street address"}, []),
    ("TRENT-ONLY", {"question": "Have you previously worked for Example Employer D?"}, []),
    ("TRENT-ONLY", {"question": "I attest that I can travel 25%."}, []),
    ("TRENT-ONLY", {"question": "Email address", "norm": "no-AI attestation"}, []),
    ("TRENT-ONLY", {"question": "Email address"}, [{"unresolved": ["What was your last manager's name?"]}]),
]


class QuestionFingerprintTests(unittest.TestCase):
    def test_ten_reviewed_families_have_at_least_three_matching_paraphrases(self):
        self.assertEqual(len(FACT_QUESTION_ALIASES), 10)
        identities = set()
        for key, variants in FACT_QUESTION_ALIASES.items():
            with self.subTest(key=key):
                self.assertGreaterEqual(len(variants), 3)
                ids = {canonical_fingerprint(question) for question in variants}
                self.assertEqual(len(ids), 1)
                self.assertEqual({fact_key(question) for question in variants}, {key})
                identities.update(ids)
        self.assertEqual(len(identities), 10)

    def test_fingerprint_is_versioned_and_normalizes_only_safe_spelling(self):
        self.assertRegex(canonical_fingerprint("Email address"), r"^qresolve:v1:[0-9a-f]{64}$")
        self.assertEqual(canonical_fingerprint("  EMAIL\n ADDRESS  "), canonical_fingerprint("Email address"))
        self.assertEqual(canonical_fingerprint("Caf\u00e9 work"), canonical_fingerprint("Cafe\u0301 work"))

    def test_qualifiers_negation_numbers_and_brackets_remain_distinct(self):
        pairs = [
            ("Email address", "Email address [for employer use only]"),
            ("Email address", "Email address [consent to marketing]"),
            ("I used AI", "I did not use AI"),
            ("Travel 25%", "Travel 50%"),
            ("AI experience in 2024", "AI experience in 2026"),
            ("1.5 years", "15 years"),
            ("2 years", "\u00b2 years"),
            ("Email address", "Business email address"),
            ("Email address", "E\u200bmail address"),
            ("First name", "First name [in native language]"),
            ("Degree A/B", "Degree AB"),
            ("[CAPTCHA] First name", "First name"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(canonical_fingerprint(left), canonical_fingerprint(right))

    def test_unknown_semantic_similarity_never_merges(self):
        variants = ["What is your favorite tool?", "Which tool do you prefer?", "Your preferred tool"]
        self.assertEqual(len({canonical_fingerprint(q) for q in variants}), 3)
        self.assertTrue(all(fact_key(q) is None for q in variants))

    def test_invalid_fingerprint_inputs_are_rejected(self):
        for value in (None, 3, [], {}, b"Email address"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                canonical_fingerprint(value)


class QuestionClassifierTests(unittest.TestCase):
    def test_synthetic_policy_fixture_set(self):
        self.assertGreaterEqual(len(SYNTHETIC_LABELED_CARDS), 50)
        self.assertEqual({row[0] for row in SYNTHETIC_LABELED_CARDS}, {"FACT", "STRUCTURAL", "JUDGMENT", "TRENT-ONLY"})
        for expected, card, contexts in SYNTHETIC_LABELED_CARDS:
            with self.subTest(card=card, expected=expected):
                result = classify(card, contexts)
                self.assertEqual(result["class"], expected)
                self.assertEqual(set(result), {"class", "confidence", "rationale", "route"})
                self.assertGreaterEqual(result["confidence"], 0)
                self.assertLessEqual(result["confidence"], 1)

    def test_apply_by_email_retraction_cannot_be_overridden_by_a_bank_hint(self):
        card = {
            "question": "Application route is apply-by-email", "bank_key": "email",
            "draft": "applicant@example.com", "confidence": 1.0,
        }
        context = [{"queue_notes": "Bank sweep falsely cleared the route; correction restored this blocker."}]
        result = classify(card, context)
        self.assertEqual(result["class"], "STRUCTURAL")
        self.assertEqual(result["route"], "structural_route")
        self.assertIsNone(fact_key(card["question"]))

    def test_structural_context_has_priority_for_every_supported_field(self):
        for field in ("question", "norm", "source", "unresolved", "queue_notes", "status_reason", "status", "blocked_reason"):
            with self.subTest(field=field):
                record = {field: ["CAPTCHA"] if field == "unresolved" else "CAPTCHA"}
                self.assertEqual(classify({"question": "Email address"}, [record])["class"], "STRUCTURAL")

    def test_human_only_never_turns_into_fact_based_on_bank_metadata(self):
        for question in ("No-AI attestation", "Interview recording consent", "Why this company?", "Unknown personal fact"):
            with self.subTest(question=question):
                self.assertEqual(classify({
                    "question": question, "bank_key": "email", "class": "FACT",
                    "draft": "approved", "confidence": 1.0,
                })["class"], "TRENT-ONLY")

    def test_commitments_are_judgment_even_with_purported_standing_answers(self):
        for question in ("Relocation willingness", "Travel availability", "Start timeframe", "Office preference"):
            with self.subTest(question=question):
                self.assertEqual(classify({"question": question, "bank_key": "relocation_willingness", "draft": "Yes"})["class"], "JUDGMENT")

    def test_mixed_unresolved_blockers_require_review(self):
        self.assertEqual(classify({"question": "Email address"}, [{"unresolved": ["Email address", "Phone number"]}])["class"], "TRENT-ONLY")
        self.assertEqual(classify({"question": "Email address"}, [{"unresolved": ["Your email address", "  "]}])["class"], "FACT")

    def test_empty_or_invalid_card_fails_closed(self):
        for card in (None, [], {}, {"question": None}, {"question": []}, {"question": "  "}):
            with self.subTest(card=card):
                self.assertEqual(classify(card)["class"], "TRENT-ONLY")

    def test_result_does_not_echo_private_context(self):
        result = classify({"question": "Email address"}, [{"queue_notes": "CAPTCHA for applicant@example.com"}])
        self.assertNotIn("applicant@example.com", json.dumps(result))

    def test_module_import_and_use_need_no_file_or_network_access(self):
        # Load the module before denying file reads, then exercise every pure
        # entry point under an audit guard; Python itself needs source reads to
        # perform imports. The import has no engine/config/workspace imports.
        code = """
import sys
from engines import qresolve_semantics as s
def guard(event, args):
    if event == 'open' or event.startswith('socket.') or event.startswith('subprocess.'):
        raise AssertionError(event)
sys.addaudithook(guard)
assert s.classify({'question': 'Email address'})['class'] == 'FACT'
assert s.fact_key('Email address') == 'email'
assert s.canonical_fingerprint('Email address').startswith('qresolve:v1:')
"""
        result = subprocess.run([sys.executable, "-B", "-S", "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
