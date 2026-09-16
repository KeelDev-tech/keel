"""Tests for monitors/blackboard.py loop registration (ARM 4, 2026-09-15).

The tooling gap: `publish` (and claim/ship/dispute/resolve) rejected unknown
--loop names with "unknown loop", while `check` accepted anything — and the
ramp coordinator could not publish under a new loop name at all. The fix:
a real `register-loop` subcommand that records name + purpose in the ledger;
publish/claim/ship/dispute/resolve validate against built-ins + registered
loops; `check` deliberately accepts anything (read-only).

Covers:
  1. register -> publish -> claim -> ship round-trips under the new loop name
     (ship must pass --metric per the ADD-1 metrics-first gate: every shipped
     proposal names its measured optimization-log metric)
  2. ship without --metric is refused fail-closed (rc=1, "no metric linked")
  3. unregistered loop fails cleanly (rc=1, "unknown loop") on publish/claim/ship/dispute/resolve
  3. the three existing loops (plus main-agent) still publish fine
  4. check still accepts any --loop string
  5. registering a built-in fails; duplicate registration fails without --force
  6. lease enforcement still applies across loop kinds (registered vs built-in)

Run: python3 test_blackboard.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
BB = os.path.abspath(os.path.join(BASE, "..", "monitors", "blackboard.py"))

# blackboard.py is internal pulse/marketing coordination tooling and is
# deliberately gitignored (see .gitignore: monitors/ "never public"). In the
# public checkout the implementation is absent, so these tests can only run
# where the working tree has it. Skipping (not deleting, not weakening) keeps
# public CI honest: a missing-implementation skip, never a silent pass.
IMPL_PRESENT = os.path.isfile(BB)

BUILTINS = {"keel-octopus-pulse", "sweep-optimization-pulse",
            "dev-support-deep-sweep", "main-agent"}


class BlackboardCase(unittest.TestCase):
    def setUp(self):
        if not IMPL_PRESENT:
            self.skipTest("monitors/blackboard.py not present (gitignored internal tooling)")
        self.tmp = tempfile.mkdtemp(prefix="bbtest-")
        self.env = dict(os.environ)
        self.env["BLACKBOARD_PATH"] = os.path.join(self.tmp, "judgment-blackboard.json")

    def bb(self, *argv):
        r = subprocess.run([sys.executable, BB, *argv], env=self.env,
                           capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(r.stdout) if r.stdout.strip() else None
        except json.JSONDecodeError:
            out = {"_raw": r.stdout}
        return r.returncode, out

    def test_register_publish_claim_ship_roundtrip(self):
        rc, out = self.bb("register-loop", "--loop", "test-roundtrip",
                           "--purpose", "regression test loop")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["loop"], "test-roundtrip")

        rc, out = self.bb("publish", "--loop", "test-roundtrip",
                           "--domain", "feed-supply", "--title", "roundtrip finding",
                           "--evidence", "e1", "--proposal-or-fix", "f1")
        self.assertEqual(rc, 0)
        eid = out["id"]

        rc, out = self.bb("claim", "--id", eid, "--loop", "test-roundtrip",
                           "--note", "working it")
        self.assertEqual(rc, 0)
        self.assertIn("lease_until", out)

        rc, out = self.bb("ship", "--id", eid, "--loop", "test-roundtrip")
        self.assertEqual(rc, 1, "ship without --metric must be refused by the metric gate")
        self.assertIn("no metric linked", out["error"])

        rc, out = self.bb("ship", "--id", eid, "--loop", "test-roundtrip",
                           "--metric", "test-roundtrip-metric")
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "shipped")

        rc, out = self.bb("show", "--status", "shipped")
        self.assertEqual(rc, 0)
        self.assertTrue(any(e["id"] == eid and e["by_loop"] == "test-roundtrip"
                            for e in out["entries"]))

    def test_unregistered_loop_rejected_cleanly(self):
        rc, out = self.bb("publish", "--loop", "no-such-loop",
                           "--domain", "feed-supply", "--title", "x")
        self.assertEqual(rc, 1)
        self.assertIn("unknown loop", out["error"])

        # claim/ship/dispute/resolve reject unknown loops too (not just publish)
        rc, out = self.bb("publish", "--loop", "keel-octopus-pulse",
                           "--domain", "feed-supply", "--title", "y")
        self.assertEqual(rc, 0)
        eid = out["id"]
        for verb in ("claim", "ship", "dispute", "resolve"):
            args = ["--id", eid, "--loop", "no-such-loop"]
            if verb == "dispute":
                args += ["--evidence", "z"]
            if verb == "resolve":
                args += ["--verdict", "accept"]
            rc, out = self.bb(verb, *args)
            self.assertEqual(rc, 1, verb)
            self.assertIn("unknown loop", out["error"], verb)

    def test_builtin_loops_still_work(self):
        for loop in BUILTINS:
            rc, out = self.bb("publish", "--loop", loop,
                               "--domain", "feed-supply",
                               "--title", f"builtin check {loop}")
            self.assertEqual(rc, 0, loop)
            self.assertTrue(out["ok"], loop)

    def test_check_accepts_any_loop(self):
        rc, out = self.bb("check", "--loop", "anything-goes-here")
        self.assertEqual(rc, 0)
        self.assertEqual(out["loop"], "anything-goes-here")

    def test_register_builtin_and_duplicate_fail(self):
        rc, out = self.bb("register-loop", "--loop", "keel-octopus-pulse")
        self.assertEqual(rc, 1)
        self.assertIn("built-in", out["error"])

        rc, _ = self.bb("register-loop", "--loop", "dup-loop", "--purpose", "v1")
        self.assertEqual(rc, 0)
        rc, out = self.bb("register-loop", "--loop", "dup-loop", "--purpose", "v2")
        self.assertEqual(rc, 1)
        self.assertIn("already registered", out["error"])
        rc, out = self.bb("register-loop", "--loop", "dup-loop",
                           "--purpose", "v2", "--force")
        self.assertEqual(rc, 0)
        rc, out = self.bb("publish", "--loop", "dup-loop",
                           "--domain", "feed-supply", "--title", "still works")
        self.assertEqual(rc, 0)

    def test_lease_enforcement_across_loop_kinds(self):
        rc, _ = self.bb("register-loop", "--loop", "lease-tester",
                         "--purpose", "lease test")
        self.assertEqual(rc, 0)
        rc, out = self.bb("publish", "--loop", "lease-tester",
                           "--domain", "feed-supply", "--title", "lease item")
        eid = out["id"]
        rc, _ = self.bb("claim", "--id", eid, "--loop", "lease-tester")
        self.assertEqual(rc, 0)
        # a built-in loop cannot steal a live lease held by the registered loop
        rc, out = self.bb("claim", "--id", eid, "--loop", "keel-octopus-pulse")
        self.assertEqual(rc, 1)
        self.assertIn("already claimed", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
