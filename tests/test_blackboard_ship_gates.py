"""Regression tests for the blackboard ship gates (K45 + K61, Keel 0.3.1 port, 2026-09-18).

K45 — a requested-but-unavailable adversarial review blocks the ship
(fail-closed) and persists an adversarial-blocked history record. Scope is
the SILENT paths only: unreadable --adversarial-diff, vacuous entry,
reviewer missing / crashed / produced no report. A reviewer that RUNS and
reports verdict="unavailable" keeps the live "unavailable never blocks"
contract (arm11 precedent) — asserted here so the scope cannot drift.

K61 tier (a) — the --metric id must name a real optimization-log record.
Invented labels are refused; the live log's schema quirks are tolerated
(nameless records skipped, duplicate names match on >=1, prose baselines
and missing evidence_sha256 ignored, unparseable lines skipped). Tier (b)
(finite-baseline / evidence-hash chain) stays unported pending a
log-schema migration.

All tests run blackboard.py as a subprocess against BLACKBOARD_PATH in a
tmp dir — the live board is never touched. KEEL_METRICS_PATH points at a
fixture log; KEEL_ADVERSARIAL_CLI points at fixture reviewer scripts, so
no paid API is ever called and no real reviewer runs.

Run: python3 test_blackboard_ship_gates.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
BB = os.path.abspath(os.path.join(BASE, "..", "monitors", "blackboard.py"))

IMPL_PRESENT = os.path.isfile(BB)

FAKE_UNAVAILABLE = """#!/usr/bin/env python3
# Mimics the live reviewer's PII-prefilter refusal shape: nonzero exit
# WITH a JSON report carrying verdict "unavailable" (arm11 precedent).
import json, sys
print(json.dumps({"verdict": "unavailable", "usecase": "code-review",
                  "gemini": {"verdict": "unavailable"},
                  "gpt": {"verdict": "unavailable"}}))
sys.exit(3)
"""

FAKE_FAIL = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"verdict": "fail", "usecase": "code-review",
                  "gemini": {"verdict": "fail", "provider": "gemini",
                             "output": "loophole: metric gate bypass"},
                  "gpt": {"verdict": "pass", "provider": "gpt"}}))
sys.exit(0)
"""

FAKE_PASS = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"verdict": "pass", "usecase": "code-review",
                  "gemini": {"verdict": "pass"},
                  "gpt": {"verdict": "pass"}}))
sys.exit(0)
"""

FAKE_CRASH = """#!/usr/bin/env python3
# Reviewer dies with garbage on stdout: no parseable report.
import sys
sys.stdout.write("Traceback (most recent call last):\\nBOOM\\n")
sys.exit(1)
"""


class ShipGatesCase(unittest.TestCase):
    def setUp(self):
        if not IMPL_PRESENT:
            self.skipTest("monitors/blackboard.py not present (gitignored internal tooling)")
        self.tmp = tempfile.mkdtemp(prefix="bbgates-")
        self.env = dict(os.environ)
        self.env["BLACKBOARD_PATH"] = os.path.join(self.tmp, "judgment-blackboard.json")
        # Fixture optimization log: mirrors the live schema quirks K61
        # tier (a) must tolerate — nameless record, duplicate name, prose
        # baseline, missing evidence_sha256, bad-JSON line, non-dict line.
        self.env["KEEL_METRICS_PATH"] = os.path.join(self.tmp, "optimization-log.jsonl")
        with open(self.env["KEEL_METRICS_PATH"], "w") as f:
            for rec in (
                {"name": "gate-test-metric", "metric": "fixture count",
                 "baseline_metric": 0, "post_metric": 1, "verdict": "compounded"},
                {"name": "dupe-metric", "metric": "first"},
                {"name": "dupe-metric", "metric": "second"},
                {"name": "prose-baseline-metric", "metric": "x",
                 "baseline_metric": "went down a lot (prose)"},
                {"metric": "nameless record, no name key"},
                {"name": "", "metric": "empty name"},
            ):
                f.write(json.dumps(rec) + "\n")
            f.write("not json {{{{\n")
            f.write("[1, 2, 3]\n")
        # Fixture reviewer scripts (no paid APIs, no network).
        self.reviewers = {}
        for key, body in (("unavailable", FAKE_UNAVAILABLE), ("fail", FAKE_FAIL),
                          ("pass", FAKE_PASS), ("crash", FAKE_CRASH)):
            path = os.path.join(self.tmp, "reviewer-%s.py" % key)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
            self.reviewers[key] = path

    def bb(self, *argv, **kw):
        env = dict(self.env)
        env.update(kw.get("env", {}))
        r = subprocess.run([sys.executable, BB, *argv], env=env,
                           capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(r.stdout) if r.stdout.strip() else None
        except json.JSONDecodeError:
            out = {"_raw": r.stdout}
        return r.returncode, out

    def board(self):
        with open(self.env["BLACKBOARD_PATH"]) as f:
            return json.load(f)

    def publish(self, title="gate fixture", **kw):
        env = kw.get("env")
        rc, out = self.bb("publish", "--loop", "main-agent",
                           "--domain", "feed-supply", "--title", title,
                           env=env or {})
        self.assertEqual(rc, 0)
        return out["id"]

    def history_events(self, eid):
        for e in self.board()["entries"]:
            if e["id"] == eid:
                return e["history"]
        self.fail("entry %s missing from board" % eid)

    # ---- K45: requested-but-unavailable review blocks ----

    def test_unreadable_adversarial_diff_blocks_and_persists_record(self):
        eid = self.publish()
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--adversarial", "--adversarial-diff",
                           os.path.join(self.tmp, "no-such-diff.patch"))
        self.assertEqual(rc, 1)
        self.assertIn("UNAVAILABLE", out["error"])
        self.assertIn("diff unreadable", out["error"])
        events = self.history_events(eid)
        blocked = [h for h in events if h["event"] == "adversarial-blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["summary"]["verdict"], "unavailable")
        self.assertTrue(all(e["status"] != "shipped"
                            for e in self.board()["entries"] if e["id"] == eid))

    def test_vacuous_entry_blocks_adversarial_ship(self):
        eid = self.publish(title="")
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--adversarial")
        self.assertEqual(rc, 1)
        self.assertIn("UNAVAILABLE", out["error"])
        self.assertIn("no change content", out["error"])
        events = self.history_events(eid)
        self.assertTrue(any(h["event"] == "adversarial-blocked"
                            for h in events))

    def test_missing_reviewer_cli_blocks(self):
        eid = self.publish()
        rc, out = self.bb(
            "ship", "--id", eid, "--loop", "main-agent", "--adversarial",
            env={"KEEL_ADVERSARIAL_CLI": os.path.join(self.tmp, "no-reviewer.py")})
        self.assertEqual(rc, 1)
        self.assertIn("UNAVAILABLE", out["error"])
        self.assertIn("no report", out["error"])
        events = self.history_events(eid)
        self.assertTrue(any(h["event"] == "adversarial-blocked" and
                            h["summary"]["verdict"] == "unavailable"
                            for h in events))

    def test_reviewer_crash_with_garbage_stdout_blocks(self):
        eid = self.publish()
        rc, out = self.bb(
            "ship", "--id", eid, "--loop", "main-agent", "--adversarial",
            env={"KEEL_ADVERSARIAL_CLI": self.reviewers["crash"]})
        self.assertEqual(rc, 1)
        self.assertIn("UNAVAILABLE", out["error"])

    def test_reviewer_reported_unavailable_still_proceeds_with_record(self):
        # Scope guard: the live "unavailable never blocks" contract covers a
        # reviewer that RAN and reported unavailable (arm11 PII-prefilter
        # precedent: nonzero exit WITH a JSON report). K45 must not change it.
        eid = self.publish()
        rc, out = self.bb(
            "ship", "--id", eid, "--loop", "main-agent",
            "--adversarial", "--metric", "gate-test-metric",
            env={"KEEL_ADVERSARIAL_CLI": self.reviewers["unavailable"]})
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "shipped")
        events = self.history_events(eid)
        reviewed = [h for h in events if h["event"] == "adversarial-reviewed"]
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0]["summary"]["verdict"], "unavailable")

    def test_reviewer_fail_still_blocks(self):
        eid = self.publish()
        rc, out = self.bb(
            "ship", "--id", eid, "--loop", "main-agent", "--adversarial",
            env={"KEEL_ADVERSARIAL_CLI": self.reviewers["fail"]})
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out["error"])
        self.assertIn("loophole", out["error"])

    def test_reviewer_pass_ships_with_metric(self):
        eid = self.publish()
        rc, out = self.bb(
            "ship", "--id", eid, "--loop", "main-agent",
            "--adversarial", "--metric", "gate-test-metric",
            env={"KEEL_ADVERSARIAL_CLI": self.reviewers["pass"]})
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "shipped")

    # ---- K61 tier (a): metric existence ----

    def test_invented_metric_refused(self):
        eid = self.publish()
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--metric", "invented-label-xyz")
        self.assertEqual(rc, 1)
        self.assertIn("not found in the optimization log", out["error"])
        self.assertIn("invented-label-xyz", out["error"])

    def test_recorded_metric_ships(self):
        eid = self.publish()
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--metric", "gate-test-metric")
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "shipped")

    def test_duplicate_metric_name_ships(self):
        # Live log has 9 duplicated names; tier (a) matches on >=1 rather
        # than the candidate's exactly-one rule.
        eid = self.publish()
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--metric", "dupe-metric")
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "shipped")

    def test_schema_quirks_do_not_break_resolution(self):
        # Nameless records, prose baselines, missing evidence_sha256, bad
        # lines: tolerated — a recorded metric still resolves, an invented
        # one is still refused.
        eid = self.publish()
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--metric", "prose-baseline-metric")
        self.assertEqual(rc, 0, "prose baselines are tier (b); tier (a) ignores them")
        eid2 = self.publish()
        rc, out = self.bb("ship", "--id", eid2, "--loop", "main-agent",
                           "--metric", "gate-test-metric")
        self.assertEqual(rc, 0)

    def test_unreadable_metric_log_fails_closed(self):
        eid = self.publish()
        rc, out = self.bb("ship", "--id", eid, "--loop", "main-agent",
                           "--metric", "gate-test-metric",
                           env={"KEEL_METRICS_PATH":
                                os.path.join(self.tmp, "no-such-log.jsonl")})
        self.assertEqual(rc, 1)
        self.assertIn("optimization log unreadable", out["error"])

    def test_resolve_accept_also_gates_metric(self):
        eid = self.publish()
        rc, out = self.bb("dispute", "--id", eid, "--loop", "main-agent",
                           "--evidence", "counter")
        self.assertEqual(rc, 0)
        rc, out = self.bb("resolve", "--id", eid, "--loop", "main-agent",
                           "--verdict", "accept", "--metric", "invented-abc")
        self.assertEqual(rc, 1)
        self.assertIn("not found in the optimization log", out["error"])
        rc, out = self.bb("resolve", "--id", eid, "--loop", "main-agent",
                           "--verdict", "accept", "--metric", "gate-test-metric")
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "shipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
