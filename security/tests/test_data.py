import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.data import dlp
from security.data.classifier import (
    Sensitivity, classify_field, classify_text, max_sensitivity)
from security.data.redactor import redact


class TestDataClassifier(unittest.TestCase):
    def test_email_field(self):
        self.assertEqual(classify_field("applicant_email"),
                         Sensitivity.PII)

    def test_password_field(self):
        self.assertEqual(classify_field("smtp_password"),
                         Sensitivity.CREDENTIAL)

    def test_amount_field(self):
        self.assertEqual(classify_field("salary_amount"),
                         Sensitivity.FINANCIAL)

    def test_unknown_field_defaults_internal(self):
        # Fail closed: unknown fields are INTERNAL, never PUBLIC.
        self.assertEqual(classify_field("favorite_color"),
                         Sensitivity.INTERNAL)

    def test_text_ssn_like_number(self):
        found = classify_text("SSN 078-05-1120")
        self.assertTrue(found)  # detected as sensitive (FINANCIAL pattern)

    def test_text_secret(self):
        self.assertIn(Sensitivity.CREDENTIAL,
                      classify_text("api_key=AKIAIOSFODNN7EXAMPLE"))

    def test_text_clean(self):
        self.assertEqual(classify_text("hello world"), set())

    def test_max_sensitivity(self):
        self.assertEqual(max_sensitivity({Sensitivity.INTERNAL,
                                          Sensitivity.PII}),
                         Sensitivity.PII)
        # Empty -> INTERNAL (fail closed, never PUBLIC).
        self.assertEqual(max_sensitivity(set()), Sensitivity.INTERNAL)

    def test_value_raises_field_sensitivity(self):
        self.assertEqual(
            classify_field("note", "AKIAIOSFODNN7EXAMPLE"),
            Sensitivity.CREDENTIAL)


class TestRedactor(unittest.TestCase):
    def test_credential_never_survives(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        red, findings = redact(f"key={secret}", level="log")
        self.assertNotIn(secret, red)
        self.assertTrue(findings)

    def test_private_key_redacted(self):
        text = ("-----BEGIN RSA PRIVATE KEY-----\nABCDEF\n"
                "-----END RSA PRIVATE KEY-----")
        red, _ = redact(text, level="strict")
        self.assertNotIn("ABCDEF", red)

    def test_ssn_redacted(self):
        red, _ = redact("ssn 078-05-1120 here", level="strict")
        self.assertNotIn("078-05-1120", red)

    def test_email_masked_even_in_log(self):
        # Defense in depth: log mode masks the local part too.
        red, _ = redact("contact bob@example.com", level="log")
        self.assertNotIn("bob@example.com", red)
        self.assertIn("example.com", red)

    def test_email_fully_redacted_strict(self):
        red, _ = redact("contact bob@example.com", level="strict")
        self.assertNotIn("bob", red)

    def test_bearer_token(self):
        tok = "Bearer eyJhbGciOiJIUzI1NiJ9.payload"
        red, _ = redact(tok, level="log")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", red)

    def test_github_token(self):
        tok = "ghp_" + "a" * 36
        red, _ = redact(tok, level="log")
        self.assertNotIn(tok, red)

    def test_phone_redacted_strict(self):
        red, _ = redact("call 415-555-0132", level="strict")
        self.assertNotIn("415-555-0132", red)

    def test_credit_card_redacted(self):
        red, _ = redact("card 4111111111111111", level="log")
        self.assertNotIn("4111111111111111", red)

    def test_clean_text_untouched(self):
        red, findings = redact("hello world", level="strict")
        self.assertEqual(red, "hello world")
        self.assertFalse(findings)


class TestDLP(unittest.TestCase):
    def test_credential_blocked_from_prompt(self):
        v = dlp.check({"api_key": Sensitivity.CREDENTIAL}, "PROMPT")
        self.assertTrue(v)
        self.assertIn("may never travel", v[0])

    def test_credential_blocked_from_internal_log(self):
        v = dlp.check({"token": Sensitivity.CREDENTIAL}, "INTERNAL_LOG")
        self.assertTrue(v)

    def test_pii_requires_justification_for_prompt(self):
        # Vendor hardening (2026-09-21): PII to PROMPT requires explicit
        # justification in context — unjustified PII in prompts is a
        # violation; justified PII remains allowed.
        v = dlp.check({"email": Sensitivity.PII}, "PROMPT")
        self.assertTrue(v)
        self.assertIn("pii_in_prompt_justified", v[0])
        self.assertEqual(dlp.check({"email": Sensitivity.PII}, "PROMPT",
                                   {"pii_in_prompt_justified": True}), [])

    def test_pii_blocked_from_external_api(self):
        v = dlp.check({"email": Sensitivity.PII}, "EXTERNAL_API")
        self.assertTrue(v)

    def test_pii_allowed_to_browser_form(self):
        # BROWSER_FORM is the approved fill path (credential actions carry
        # their own approval context); PII itself is not barred there.
        self.assertEqual(dlp.check({"email": Sensitivity.PII},
                                   "BROWSER_FORM"), [])

    def test_unknown_destination_deny(self):
        v = dlp.check({"email": Sensitivity.PII}, "mystery_place")
        self.assertTrue(v)
        self.assertIn("fail closed", v[0])

    def test_public_allowed_to_internal_log(self):
        self.assertEqual(dlp.check({"note": Sensitivity.PUBLIC},
                                   "INTERNAL_LOG"), [])

    def test_may_travel_ok(self):
        ok, _ = dlp.may_travel(Sensitivity.INTERNAL, "ARTIFACT")
        self.assertTrue(ok)

    def test_financial_to_log_requires_redaction_flag(self):
        ok, _ = dlp.may_travel(Sensitivity.FINANCIAL, "INTERNAL_LOG",
                               {"redacted": False})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
