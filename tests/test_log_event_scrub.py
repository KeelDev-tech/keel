"""Tests for log_event.py -- scrub() secret redaction.

Contract under test (documented on scrub() itself, Keel 0.11 port 2026-09-18):
  1. Every key in SECRET_KEYS is REDACTED: the key is kept, the value becomes
     '[REDACTED]'. This replaced the previous top-level-only drop.
  2. Secret-key matching is case-insensitive (e.g., 'PASSWORD', 'Token',
     'ApiKey') and also matches secret fragments inside longer keys
     (e.g., 'my_password' contains 'password').
  3. Redaction recurses into nested dicts and lists.
  4. 'Bearer <token>' and 'key=value' secret patterns inside plain strings
     are redacted as well.
  5. Non-secret keys pass through untouched with original values preserved.
  6. The input dictionary is not mutated (scrub returns a new copy).
  7. Empty dict handling (returns empty dict).
  8. Non-string keys are safely handled and preserved if non-secret.
  9. Non-finite floats, unsupported value types, and excessive nesting raise
     ValueError.

Run: python3 -m unittest discover -s tests  (or python3 tests/test_log_event_scrub.py)
"""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import log_event


class TestLogEventScrub(unittest.TestCase):
    def test_every_secret_key_is_redacted(self):
        """Every key in SECRET_KEYS keeps its key with value '[REDACTED]'."""
        for secret_key in log_event.SECRET_KEYS:
            with self.subTest(secret_key=secret_key):
                cleaned = log_event.scrub({secret_key: "sensitive_value_123"})
                self.assertEqual(cleaned, {secret_key: "[REDACTED]"})

    def test_case_insensitive_matching(self):
        """Secret-key matching must be case-insensitive."""
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
                cleaned = log_event.scrub({key: val})
                self.assertEqual(cleaned, {key: "[REDACTED]"})

    def test_secret_fragment_inside_longer_key(self):
        """Keys merely containing a secret fragment are redacted too."""
        cleaned = log_event.scrub({"my_password_hash": "x", "user_token_v2": "y"})
        self.assertEqual(
            cleaned, {"my_password_hash": "[REDACTED]", "user_token_v2": "[REDACTED]"}
        )

    def test_non_secret_keys_pass_through_untouched(self):
        """Non-secret keys must be preserved with unchanged values and types."""
        safe_data = {
            "role_id": "TEST-ROLE-42",
            "company": "ExampleCorp",
            "gate": "captcha",
            "ats": "greenhouse",
            "attempts": 3,
            "success": True,
            "nothing": None,
            "metadata": {"nested": "value"},
            "tags": ["remote", "swe"],
        }
        cleaned = log_event.scrub(safe_data)
        self.assertEqual(cleaned, safe_data)

    def test_input_dict_is_not_mutated(self):
        """scrub() must return a new copy and not mutate the caller's dictionary."""
        original = {
            "password": "super-secret-value",
            "role_id": "ROLE-100",
            "status": "pending",
        }
        snapshot = dict(original)
        cleaned = log_event.scrub(original)

        self.assertEqual(original, snapshot, "Input dict was mutated in-place")
        self.assertIsNot(cleaned, original, "scrub() must return a distinct dictionary")
        self.assertEqual(
            cleaned,
            {"password": "[REDACTED]", "role_id": "ROLE-100", "status": "pending"},
        )

    def test_nested_structures_are_redacted(self):
        """Redaction recurses into nested dicts and lists."""
        payload = {
            "outer": {"token": "abc", "safe": 1},
            "items": [{"secret": "s"}, "plain"],
        }
        cleaned = log_event.scrub(payload)
        self.assertEqual(
            cleaned,
            {
                "outer": {"token": "[REDACTED]", "safe": 1},
                "items": [{"secret": "[REDACTED]"}, "plain"],
            },
        )

    def test_secret_patterns_inside_strings(self):
        """Bearer tokens and key=value secret patterns inside strings are redacted."""
        cleaned = log_event.scrub(
            {"header": "Bearer abcDEF123", "dsn": "user=demo&password=hunter2 ok"}
        )
        self.assertEqual(cleaned["header"], "Bearer [REDACTED]")
        self.assertIn("password=[REDACTED]", cleaned["dsn"])
        self.assertNotIn("hunter2", cleaned["dsn"])

    def test_empty_dict(self):
        """An empty details dict should return an empty dict."""
        self.assertEqual(log_event.scrub({}), {})

    def test_mixed_payload(self):
        """In a mixed dictionary, secret keys are redacted; safe keys remain."""
        mixed = {
            "role_id": "ROLE-99",
            "password": "my_password",
            "gate": "needs_input",
            "OTP": "123456",
            "valid": True,
        }
        cleaned = log_event.scrub(mixed)
        self.assertEqual(
            cleaned,
            {
                "role_id": "ROLE-99",
                "password": "[REDACTED]",
                "gate": "needs_input",
                "OTP": "[REDACTED]",
                "valid": True,
            },
        )

    def test_non_string_keys(self):
        """Non-string keys should be safely handled and preserved if not secret."""
        payload = {101: "error_code_101", "status": "ok"}
        cleaned = log_event.scrub(payload)
        self.assertEqual(cleaned, payload)

    def test_non_finite_float_raises(self):
        """NaN / infinite telemetry values are rejected."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    log_event.scrub({"v": bad})

    def test_unsupported_type_raises(self):
        """Value types outside dict/list/str/bool/int/float/None are rejected."""
        with self.assertRaises(ValueError):
            log_event.scrub({"v": {"a", "set"}})

    def test_excessive_nesting_raises(self):
        """Nesting deeper than the bounded recursion limit is rejected."""
        deep = {}
        cursor = deep
        for _ in range(20):
            cursor["n"] = {}
            cursor = cursor["n"]
        with self.assertRaises(ValueError):
            log_event.scrub(deep)


if __name__ == "__main__":
    unittest.main()
