#!/usr/bin/env python3
"""Tests for input_resolution/preferences.py -- standing-preference architecture.

unittest style (repo CI: python3 -m unittest discover -s tests).
Covers: public API surface, example-only seeds (no personal data), scope
precedence, exceptions, fail-closed expiry, the APPLICANT_ONLY_FAMILIES
contract, the blocker.py consumer contract, and a negative assertion that
no personal identifiers appear in the input_resolution sources.

Run: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)

from input_resolution import preferences as prefs  # noqa: E402
from input_resolution import blocker  # noqa: E402

IR_DIR = os.path.join(ENGINES, "input_resolution")

# Example identifiers that must never appear in the public sources.
# Synthetic stand-ins: the guard scans for PII-shaped leaks without embedding
# any real personal identifier in the published history.
FORBIDDEN = (
    "alex.applicant1",
    "Alex Applicant Doe",
    "APPLICANTCORP",
    "999",
    "00000",
    "@example.invalid",
    "Alex",
)


class TestPreferences(unittest.TestCase):
    def test_import_exposes_public_api(self):
        for name in ("Preference", "is_expired", "match_preference",
                     "seed_preferences", "APPLICANT_ONLY_FAMILIES"):
            self.assertTrue(hasattr(prefs, name), name)
        self.assertFalse(hasattr(prefs, "TRENT_ONLY_FAMILIES"),
                         "old personal-named constant must be gone")

    def test_seed_is_example_only(self):
        seeds = prefs.seed_preferences()
        self.assertGreaterEqual(len(seeds), 5)
        for s in seeds:
            blob = f"{s.value} {s.source}".lower()
            self.assertIn("example", blob,
                          f"example seeds must be marked as examples: {s.family}")

    def test_no_personal_identifiers_in_sources(self):
        hits = []
        for fname in sorted(os.listdir(IR_DIR)):
            if not fname.endswith(".py"):
                continue
            with open(os.path.join(IR_DIR, fname), encoding="utf-8") as f:
                text = f.read()
            for ident in FORBIDDEN:
                if ident in text:
                    hits.append(f"{fname}: {ident!r}")
        self.assertEqual(hits, [],
                         "personal identifier leaked into public sources")

    def test_match_most_specific_scope_wins(self):
        ps = [
            prefs.Preference("f.travel", "global-yes"),
            prefs.Preference("f.travel", "employer-no",
                             scope="employer:acme"),
        ]
        p, reason = prefs.match_preference(ps, "f.travel", employer="Acme Corp")
        self.assertEqual(p.value, "employer-no", reason)
        p2, _ = prefs.match_preference(ps, "f.travel", employer="Other Inc")
        self.assertEqual(p2.value, "global-yes")

    def test_exceptions_beat_scope(self):
        ps = [prefs.Preference("f.travel", "global-yes",
                               exceptions=("employer:acme",))]
        p, reason = prefs.match_preference(ps, "f.travel", employer="Acme")
        self.assertIsNone(p)
        self.assertEqual(reason, "no standing preference for family", reason)

    def test_expired_preference_fails_closed(self):
        yesterday = (datetime.now(timezone.utc)
                     - timedelta(days=1)).date().isoformat()
        ps = [prefs.Preference("f.travel", "global-yes", expires_at=yesterday)]
        p, reason = prefs.match_preference(ps, "f.travel")
        self.assertIsNone(p)
        self.assertIn("expired", reason, reason)
        # Unparseable expiry also fails closed.
        ps2 = [prefs.Preference("f.travel", "global-yes", expires_at="never")]
        self.assertTrue(prefs.is_expired(ps2[0]))

    def test_applicant_only_families_never_auto_resolve(self):
        self.assertIn("legal.arbitration", prefs.APPLICANT_ONLY_FAMILIES)
        self.assertIn("interview_recording.general",
                      prefs.APPLICANT_ONLY_FAMILIES)
        self.assertIn("no_ai_unaided_writing", prefs.APPLICANT_ONLY_FAMILIES)

    def test_blocker_consumes_preferences_module(self):
        # blocker.py does `from . import preferences`; this is the contract
        # the engine depends on.
        self.assertIs(blocker.prefs_mod, prefs)
        self.assertIn("legal.arbitration",
                      blocker.prefs_mod.APPLICANT_ONLY_FAMILIES)

    def test_check_07_applicant_only_family_survives(self):
        b = blocker.Blocker(lead_id="L1", company="Acme", role="Ops",
                            raw_blocker="Do you consent to binding arbitration?",
                            verified_current_form=True)
        ctx = blocker.ProofContext(answer_bank={},
                                   preferences=prefs.seed_preferences(),
                                   candidate_records={})
        b = blocker.run_proof(b, ctx)
        self.assertTrue(b.requires_applicant)
        self.assertEqual(b.classification, "legal_attestation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
