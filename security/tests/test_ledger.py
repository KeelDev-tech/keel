import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.ledger.events import SecurityLedger
from security.ledger.evidence import (
    bundle_evidence, hash_text, verify_bundle)
from security.ledger.hashchain import HashChain
from security.policy.engine import Decision


def _tmp_path():
    d = tempfile.mkdtemp(prefix="keel-ledger-test-")
    return os.path.join(d, "events.jsonl")


class TestHashChain(unittest.TestCase):
    def test_append_and_verify(self):
        hc = HashChain()
        r1 = hc.append({"a": 1})
        r2 = hc.append({"b": 2})
        self.assertEqual(r1["seq"], 0)
        self.assertEqual(r2["prev_hash"], r1["hash"])
        ok, _ = hc.verify()
        self.assertTrue(ok)

    def test_tamper_detected(self):
        hc = HashChain()
        hc.append({"a": 1})
        hc.append({"b": 2})
        hc._records[0]["body"]["a"] = 999  # tamper
        ok, detail = hc.verify()
        self.assertFalse(ok)
        self.assertIn("hash mismatch", detail)

    def test_deletion_detected(self):
        hc = HashChain()
        hc.append({"a": 1})
        hc.append({"b": 2})
        del hc._records[0]
        ok, detail = hc.verify()
        self.assertFalse(ok)

    def test_reorder_detected(self):
        hc = HashChain()
        hc.append({"a": 1})
        hc.append({"b": 2})
        hc._records.reverse()
        ok, _ = hc.verify()
        self.assertFalse(ok)


class TestSecurityLedger(unittest.TestCase):
    def setUp(self):
        self.path = _tmp_path()
        self.ledger = SecurityLedger(self.path)

    def test_record_and_verify(self):
        rec = self.ledger.record(
            agent_id="agent-1", action_name="read_leads",
            decision=Decision.ALLOW, reasons=["ok"])
        self.assertEqual(rec["seq"], 0)
        self.assertIn("hash", rec)
        self.assertEqual(rec["body"]["decision"], "ALLOW")
        ok, _ = self.ledger.verify()
        self.assertTrue(ok)

    def test_persists_to_file(self):
        self.ledger.record(agent_id="a", action_name="x",
                           decision=Decision.DENY, reasons=["no"])
        with open(self.path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        envelope = json.loads(lines[0])
        self.assertEqual(envelope["body"]["decision"], "DENY")
        self.assertEqual(envelope["seq"], 0)

    def test_reload_verifies(self):
        self.ledger.record(agent_id="a", action_name="x",
                           decision=Decision.ALLOW, reasons=[])
        self.ledger.record(agent_id="a", action_name="y",
                           decision=Decision.DENY, reasons=["r"])
        fresh = SecurityLedger(self.path)
        ok, detail = fresh.verify()
        self.assertTrue(ok)
        self.assertIn("2 events", detail)

    def test_file_tamper_detected(self):
        self.ledger.record(agent_id="a", action_name="x",
                           decision=Decision.ALLOW, reasons=[])
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"forged": true}\n')  # append-only: no valid chain
        fresh = SecurityLedger(self.path)
        ok, _ = fresh.verify()
        self.assertFalse(ok)

    def test_denials_are_ledgered(self):
        rec = self.ledger.record(
            agent_id="a", action_name="api_direct_write",
            decision=Decision.DENY, reasons=["blocker"])
        self.assertEqual(rec["body"]["decision"], "DENY")


class TestEvidence(unittest.TestCase):
    def test_hash_text_stable(self):
        self.assertEqual(hash_text("x"), hash_text("x"))
        self.assertNotEqual(hash_text("x"), hash_text("y"))

    def _bundle(self):
        return bundle_evidence(
            agent_id="agent-1", action_name="record_submission",
            input_hashes={"note": hash_text("confirmed by ATS")},
            policy_version="2026-09-18.1", decision="ALLOW",
            reasons=["evidence presented"])

    def test_bundle_roundtrip(self):
        self.assertTrue(verify_bundle(self._bundle()))

    def test_bundle_tamper_detected(self):
        b = self._bundle()
        b["reasons"] = ["forged"]
        self.assertFalse(verify_bundle(b))

    def test_bundle_missing_hash(self):
        b = self._bundle()
        del b["bundle_hash"]
        self.assertFalse(verify_bundle(b))


if __name__ == "__main__":
    unittest.main()
