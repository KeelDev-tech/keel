"""Tests for log_event.py -- scrub() secret-key filtering.

Covers:
  1. Every key in SECRET_KEYS is dropped when scrubbed.
  2. Secret key matching is case-insensitive (e.g., 'PASSWORD', 'Token', 'ApiKey').
  3. Non-secret keys pass through untouched with their original values preserved.
  4. The input dictionary is not mutated (scrub returns a new copy).
  5. A dropped secret key emits the expected warning to sys.stderr.
  6. Clean dictionaries emit no warnings to sys.stderr.
  7. Empty dictionary handling (returns empty dict, no warnings).
  8. Non-string keys are safely handled and preserved if non-secret.

Run: python3 -m unittest discover -s tests  (or python3 tests/test_log_event_scrub.py)
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import log_event


class TestLogEventScrub(unittest.TestCase):
    def test_every_secret_key_is_dropped(self):
        """Verify that every single key in SECRET_KEYS is removed by scrub()."""
        for secret_key in log_event.SECRET_KEYS:
            with self.subTest(secret_key=secret_key):
                payload = {secret_key: "sensitive_value_123"}
                with redirect_stderr(io.StringIO()):
                    cleaned = log_event.scrub(payload)
                self.assertNotIn(
                    secret_key,
                    cleaned,
                    f"Secret key '{secret_key}' was not dropped by scrub()",
                )
                self.assertEqual(cleaned, {})

    def test_case_insensitive_matching(self):
        """Secret key matching must be case-insensitive."""
        variations = {
            "PASSWORD": "p1",
            "Pass": "p2",
            "PASSWD": "p3",
            "TOKEN": "t1",
            "Token": "t2",
            "SECRET": "s1",
            "API_KEY": "k1",
            "Api_Key": "k2",
            "APIKEY": "k3",
            "VERIFICATION_CODE": "c1",
            "Verify_Code": "c2",
            "OTP": "o1",
            "ONE_TIME_CODE": "o2",
            "CODE": "c3",
            "CREDENTIAL": "cr1",
            "Credentials": "cr2",
            "SESSION": "s2",
            "COOKIE": "ck1",
            "AUTH": "a1",
        }
        for key, val in variations.items():
            with self.subTest(key=key):
                payload = {key: val}
                with redirect_stderr(io.StringIO()):
                    cleaned = log_event.scrub(payload)
                self.assertNotIn(
                    key,
                    cleaned,
                    f"Case variation '{key}' was not dropped by scrub()",
                )
                self.assertEqual(cleaned, {})

    def test_non_secret_keys_pass_through_untouched(self):
        """Non-secret keys must be preserved with unchanged values and types."""
        safe_data = {
            "role_id": "TEST-ROLE-42",
            "company": "ExampleCorp",
            "gate": "captcha",
            "ats": "greenhouse",
            "attempts": 3,
            "success": True,
            "metadata": {"nested": "value"},
            "tags": ["remote", "swe"],
        }
        with redirect_stderr(io.StringIO()) as stderr:
            cleaned = log_event.scrub(safe_data)
        self.assertEqual(cleaned, safe_data)
        self.assertEqual(stderr.getvalue(), "")

    def test_input_dict_is_not_mutated(self):
        """scrub() must return a new copy and not mutate the caller's dictionary."""
        original = {
            "password": "super-secret-value",
            "role_id": "ROLE-100",
            "status": "pending",
        }
        snapshot = dict(original)
        with redirect_stderr(io.StringIO()):
            cleaned = log_event.scrub(original)

        self.assertEqual(original, snapshot, "Input dict was mutated in-place")
        self.assertIsNot(cleaned, original, "scrub() must return a distinct dictionary")
        self.assertEqual(cleaned, {"role_id": "ROLE-100", "status": "pending"})

    def test_dropped_key_emits_stderr_warning(self):
        """Dropping a secret key must print a telemetry warning to sys.stderr."""
        payload = {"token": "xyz-token-abc"}
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            log_event.scrub(payload)

        output = stderr_buf.getvalue()
        self.assertIn("telemetry: dropped secret-looking detail key 'token' (not logged)", output)

    def test_empty_dict(self):
        """An empty details dict should return an empty dict without emitting warnings."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            cleaned = log_event.scrub({})
        self.assertEqual(cleaned, {})
        self.assertEqual(stderr_buf.getvalue(), "")

    def test_mixed_payload(self):
        """In a mixed dictionary, only secret keys are dropped; safe keys remain."""
        mixed = {
            "role_id": "ROLE-99",
            "password": "my_password",
            "gate": "needs_input",
            "OTP": "123456",
            "valid": True,
        }
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            cleaned = log_event.scrub(mixed)

        expected = {
            "role_id": "ROLE-99",
            "gate": "needs_input",
            "valid": True,
        }
        self.assertEqual(cleaned, expected)
        warning_output = stderr_buf.getvalue()
        self.assertIn("'password'", warning_output)
        self.assertIn("'OTP'", warning_output)

    def test_non_string_keys(self):
        """Non-string keys should be safely handled and preserved if not secret."""
        payload = {101: "error_code_101", "status": "ok"}
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            cleaned = log_event.scrub(payload)
        self.assertEqual(cleaned, payload)
        self.assertEqual(stderr_buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
