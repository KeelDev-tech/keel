"""Regression: classification taxonomy, default-deny, content overrides."""

import os
import sys
import unittest

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy.classifier import (
    ArtifactClass, classify, assert_valid_class, EGRESS_DENY, VALID_CLASSES,
)


class TestClassifier(unittest.TestCase):
    def test_exactly_nine_classes(self):
        self.assertEqual(len(VALID_CLASSES), 9)
        self.assertEqual(
            sorted(VALID_CLASSES),
            sorted(["SOURCE_CODE", "DOCUMENTATION", "AGGREGATE_METRICS",
                    "TELEMETRY", "APPLICATION_DATA", "SECURITY_EVIDENCE",
                    "PERSONAL_DATA", "SECRETS", "DERIVED_DATA"]))

    def test_structural_classification(self):
        self.assertEqual(classify("app.py").artifact_class, "SOURCE_CODE")
        self.assertEqual(classify("README.md").artifact_class, "DOCUMENTATION")
        self.assertEqual(classify("events.jsonl").artifact_class, "TELEMETRY")
        self.assertEqual(classify("audit-evidence.pdf").artifact_class,
                         "SECURITY_EVIDENCE")
        self.assertEqual(classify("metrics-rollup.csv").artifact_class,
                         "AGGREGATE_METRICS")

    def test_default_deny_on_every_class(self):
        # No classification may ever authorize egress.
        for cls in VALID_CLASSES:
            r = classify("whatever.bin", content=b"\x00\x01\x02")
            self.assertEqual(r.external_egress, EGRESS_DENY)
        # Unclassifiable material: conservative fallback, still DENY.
        r = classify("mystery.xyzzy", media_type="application/octet-stream",
                     content=b"\x00\x01binary")
        self.assertEqual(r.artifact_class, "APPLICATION_DATA")
        self.assertEqual(r.external_egress, EGRESS_DENY)
        self.assertTrue(any("fallback" in x for x in r.reasons))

    def test_unclassified_without_content_still_deny(self):
        r = classify("noextension")
        self.assertEqual(r.external_egress, EGRESS_DENY)

    def test_pii_overrides_structure(self):
        r = classify("notes.md", content=b"contact jane.doe@example.com for access")
        self.assertEqual(r.artifact_class, "PERSONAL_DATA")
        self.assertEqual(r.external_egress, EGRESS_DENY)
        self.assertEqual(len(r.pii_findings), 1)
        # Findings never carry raw values.
        self.assertNotIn("jane.doe@example.com",
                         str(r.pii_findings[0].masked_samples))

    def test_secrets_override_pii_and_structure(self):
        content = (b"report for jane.doe@example.com\n"
                   b"api_key = 'AKIAIOSFODNN7EXAMPLE'\n")
        r = classify("report.md", content=content)
        self.assertEqual(r.artifact_class, "SECRETS")
        self.assertEqual(r.external_egress, EGRESS_DENY)
        self.assertTrue(r.secret_findings)
        joined = str(r.secret_findings[0].masked_samples)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", joined)

    def test_private_key_block_detected(self):
        r = classify("notes.txt",
                     content=b"-----BEGIN RSA PRIVATE KEY-----\nMIIB...")
        self.assertEqual(r.artifact_class, "SECRETS")

    def test_binary_content_no_crash(self):
        r = classify("model.bin", content=os.urandom(256))
        self.assertEqual(r.external_egress, EGRESS_DENY)

    def test_assert_valid_class_rejects_unknown(self):
        with self.assertRaises(ValueError):
            assert_valid_class("TOTALLY_MADE_UP")
        self.assertEqual(assert_valid_class("TELEMETRY"), "TELEMETRY")

    def test_result_serializes(self):
        d = classify("a.py", content=b"print(1)").to_dict()
        self.assertEqual(d["external_egress"], "DENY")
        self.assertIn("artifact_class", d)


if __name__ == "__main__":
    unittest.main()
