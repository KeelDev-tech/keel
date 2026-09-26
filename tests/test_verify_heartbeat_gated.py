"""gated_no_eligible classification tests for verify_heartbeat (2026-09-19).

The silent metric lie: a targeted verify run whose cohort was entirely
eligibility-blocked correctly aborts before any HTTP and records
status="gated_no_eligible" — a healthy, deliberate no-op. The old
classify_run mapped every status != "ok" to ERROR, and decide() gates
IMMEDIATELY on any ERROR run, so each gate abort fired a transport-failure
alert. A deliberate pre-gate abort is not a transport failure.

The fix: classify_run returns the new GATED class for gated_no_eligible;
decide() treats GATED as streak-neutral (like IGNORE/NO_OPPORTUNITY) —
it neither triggers error_status nor advances the zero-verdict streak.

Covers:
  1. gated_no_eligible classifies GATED, not ERROR
  2. a lone gated run -> decide() QUIET, no error_status trigger
  3. gated runs do not advance the zero-verdict streak
  4. gated runs do not reset an in-progress zero-verdict streak either
     (streak-neutral means neutral: neither advance nor reset)
  5. genuine error statuses still gate immediately (regression)
  6. ok zero-verdict runs still gate on streak (regression)

Run: python3 test_verify_heartbeat_gated.py
"""
import importlib.util
import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
MON = os.path.abspath(os.path.join(BASE, "..", "monitors"))
sys.path.insert(0, MON)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(MON, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hb = _load("verify_heartbeat")


def rec(status, scanned=0, pool=1597, live=0, dead=0):
    return {"ts": "2026-09-19T00:00:00Z", "live": True, "limit": 200,
            "status": status,
            "counts": {"scanned": scanned, "verified_live": live,
                       "dead": dead, "pool_size": pool}}


def classified(*recs):
    return [(r["ts"], hb.classify_run(r), r) for r in recs]


class TestGatedClassification(unittest.TestCase):
    def test_gated_not_error(self):
        r = rec("gated_no_eligible")
        self.assertEqual(hb.classify_run(r), hb.GATED)
        self.assertNotEqual(hb.classify_run(r), hb.ERROR)

    def test_lone_gated_run_is_quiet(self):
        d = hb.decide(classified(rec("gated_no_eligible")))
        self.assertEqual(d["verdict"], "QUIET")
        self.assertIsNone(d["trigger"])
        self.assertEqual(d["error_runs"], [])

    def test_gated_does_not_advance_streak(self):
        # zero-verdict, gated, zero-verdict: streak must be 2, not 3.
        # (N=1 on this host, so the verdict is GATE either way; the streak
        # value is what proves the gated run didn't advance it.)
        runs = classified(
            rec("ok", scanned=50), rec("gated_no_eligible"),
            rec("ok", scanned=50))
        d = hb.decide(runs)
        self.assertEqual(d["verdict"], "GATE")
        self.assertEqual(d["streak"], 2)

    def test_gated_does_not_reset_streak(self):
        # verdict, zero-verdict, gated: the zero-verdict streak survives
        # the neutral gated run (streak 1 — neither reset to 0 nor
        # advanced to 2).
        runs = classified(
            rec("ok", scanned=50, live=10), rec("ok", scanned=50),
            rec("gated_no_eligible"))
        d = hb.decide(runs)
        self.assertEqual(d["verdict"], "GATE")
        self.assertEqual(d["streak"], 1)

    def test_genuine_error_still_gates(self):
        d = hb.decide(classified(rec("transport_error")))
        self.assertEqual(d["verdict"], "GATE")
        self.assertEqual(d["trigger"], "error_status")

    def test_zero_verdict_streak_still_gates(self):
        # N=1 on this host: a single zero-verdict run gates (existing rule).
        d = hb.decide(classified(rec("ok", scanned=50)))
        self.assertEqual(d["verdict"], "GATE")
        self.assertEqual(d["trigger"], "zero_verdict_streak")


if __name__ == "__main__":
    unittest.main(verbosity=2)
