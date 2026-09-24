import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.errors import SecretViolation
from security.identity.agent_identity import (
    IdentityRegistry, mint_identity)
from security.secrets.broker import SecretsBroker
from security.secrets.scanner import scan_file, scan_text


class TestSecretsBroker(unittest.TestCase):
    def setUp(self):
        IdentityRegistry.clear()
        self.broker = SecretsBroker()
        self.agent = mint_identity("agent", "worker-1")
        IdentityRegistry.register(self.agent)

    def tearDown(self):
        IdentityRegistry.clear()

    def test_issue_and_use(self):
        cred = self.broker.issue(self.agent.id, "browser_login",
                                 "ats:greenhouse", ttl_seconds=300)
        used = self.broker.use(cred.token_id, self.agent.id)
        self.assertEqual(used.token_id, cred.token_id)
        self.assertEqual(len(self.broker.active_for(self.agent.id)), 1)

    def test_wrong_agent_refused(self):
        cred = self.broker.issue(self.agent.id, "browser_login",
                                 "ats:greenhouse", ttl_seconds=300)
        other = mint_identity("agent", "worker-2")
        with self.assertRaises(SecretViolation):
            self.broker.use(cred.token_id, other.id)

    def test_wildcard_scope_refused(self):
        with self.assertRaises(SecretViolation):
            self.broker.issue(self.agent.id, "p", "*", ttl_seconds=60)

    def test_permanent_ttl_refused(self):
        with self.assertRaises(SecretViolation):
            self.broker.issue(self.agent.id, "p", "a:b",
                              ttl_seconds=7200)
        with self.assertRaises(SecretViolation):
            self.broker.issue(self.agent.id, "p", "a:b", ttl_seconds=0)

    def test_single_use(self):
        cred = self.broker.issue(self.agent.id, "p", "a:b",
                                 ttl_seconds=300, single_use=True)
        self.broker.use(cred.token_id, self.agent.id)
        with self.assertRaises(SecretViolation):
            self.broker.use(cred.token_id, self.agent.id)

    def test_multi_use_when_not_single(self):
        cred = self.broker.issue(self.agent.id, "p", "a:b",
                                 ttl_seconds=300, single_use=False)
        self.broker.use(cred.token_id, self.agent.id)
        self.broker.use(cred.token_id, self.agent.id)  # no raise

    def test_expired_handle(self):
        cred = self.broker.issue(self.agent.id, "p", "a:b",
                                 ttl_seconds=300)
        import datetime
        cred.expires_at = (datetime.datetime.now(
            datetime.timezone.utc)
            - datetime.timedelta(seconds=1)).isoformat()
        with self.assertRaises(SecretViolation):
            self.broker.use(cred.token_id, self.agent.id)

    def test_revoke(self):
        cred = self.broker.issue(self.agent.id, "p", "a:b",
                                 ttl_seconds=300)
        self.assertTrue(self.broker.revoke(cred.token_id, "test"))
        with self.assertRaises(SecretViolation):
            self.broker.use(cred.token_id, self.agent.id)

    def test_unknown_handle(self):
        with self.assertRaises(SecretViolation):
            self.broker.use("ksec_nonexistent", self.agent.id)

    def test_no_raw_material_exposed(self):
        cred = self.broker.issue(self.agent.id, "p", "a:b",
                                 ttl_seconds=300)
        self.assertFalse(hasattr(cred, "material"))
        self.assertNotIn("material", cred.__dict__)

    def test_empty_scope_refused(self):
        with self.assertRaises(SecretViolation):
            self.broker.issue(self.agent.id, "p", "", ttl_seconds=60)

    def test_empty_purpose_refused(self):
        with self.assertRaises(SecretViolation):
            self.broker.issue(self.agent.id, "", "a:b", ttl_seconds=60)


class TestSecretScanner(unittest.TestCase):
    def test_aws_key(self):
        res = scan_text("key = AKIAIOSFODNN7EXAMPLE")
        self.assertFalse(res.is_clean)
        self.assertTrue(any("aws" in f.rule_id for f in res.findings))

    def test_excerpt_redacted(self):
        tok = "ghp_" + "a" * 36
        res = scan_text(f"token={tok}")
        self.assertFalse(res.is_clean)
        for f in res.findings:
            self.assertNotIn(tok, f.excerpt)

    def test_private_key(self):
        res = scan_text("-----BEGIN RSA PRIVATE KEY-----\nXYZ")
        self.assertFalse(res.is_clean)

    def test_generic_assignment(self):
        res = scan_text('password = "supersecretvalue123"')
        self.assertFalse(res.is_clean)

    def test_clean_text(self):
        self.assertTrue(scan_text(
            "the pipeline ran successfully").is_clean)

    def test_scan_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as f:
            f.write('TOKEN = "xoxb-123456789012-abcdef"\n')
            path = f.name
        try:
            res = scan_file(path)
            self.assertFalse(res.is_clean)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
