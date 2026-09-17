#!/usr/bin/env python3
"""stdlib unittest for the newly ported input-resolution + verification +
outcome modules.

Synthetic data only. Covers: dedup collapse semantics (portable vs
non-portable families, unknown-family integrity rule), metrics tap-list
counting with the renamed fields, apply helpers (family_entry, qn_with_notes,
blocker_raws), dry_run generic fixtures, recover_watermark repair logic, and
learning_leak_audit capture math against the evidence gate.
"""
import json
import os
import sys
import tempfile
import unittest

_ENGINES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engines")
_ENGINES = os.path.normpath(_ENGINES)
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)

from input_resolution import blocker as B
from input_resolution import dedup as D
from input_resolution import metrics as M
from input_resolution import preferences as P
from input_resolution import apply as A
from input_resolution import dry_run as R
from input_resolution import recover_watermark as RW
from outcome_tracking import learning_leak_audit as LLA


def _mk(lead_id, family, classification, raw="raw text",
        requires_applicant=False, company="Acme", role="Role"):
    b = B.Blocker(lead_id=lead_id, company=company, role=role,
                  raw_blocker=raw)
    b.normalized_family = family
    b.family_variant = "default"
    b.classification = classification
    b.requires_applicant = requires_applicant
    return b


class DedupTest(unittest.TestCase):
    def test_portable_family_collapses(self):
        bs = [_mk("L1", "travel_commitment", "user_preference",
                  requires_applicant=True),
              _mk("L2", "travel_commitment", "user_preference",
                  requires_applicant=True)]
        fams = D.collapse(bs)
        self.assertEqual(len(fams), 1)
        self.assertEqual(sorted(fams[0].blocker_ids), ["L1", "L2"])
        self.assertTrue(fams[0].requires_applicant)
        self.assertEqual(bs[1].classification, "duplicate")
        self.assertFalse(bs[1].requires_applicant)

    def test_unknown_family_never_collapses(self):
        bs = [_mk("L1", "unknown", "user_fact", raw="weird thing one",
                  requires_applicant=True),
              _mk("L2", "unknown", "user_fact", raw="weird thing two",
                  requires_applicant=True)]
        fams = D.collapse(bs)
        self.assertEqual(len(fams), 2)
        self.assertEqual({f.blocker_ids[0] for f in fams}, {"L1", "L2"})

    def test_resolved_never_enters_families(self):
        bs = [_mk("L1", "travel_commitment", "resolved_auto"),
              _mk("L2", "travel_commitment", "resolved_auto")]
        fams = D.collapse(bs)
        self.assertEqual(fams, [])

    def test_decision_prompt_uses_applicant_language(self):
        prompt = D.DECISION_PROMPTS["apply_by_email_authorization"]
        self.assertIn("applicant", prompt)
        self.assertNotIn("Trent", prompt)


class MetricsTest(unittest.TestCase):
    def test_tap_list_counts_applicant_families_once(self):
        bs = [_mk("L1", "travel_commitment", "user_preference",
                  requires_applicant=True),
              _mk("L2", "travel_commitment", "duplicate")]
        fams = D.collapse(bs)
        rep = M.compute(bs, fams)
        self.assertEqual(rep.true_applicant_decisions, 1)
        self.assertGreater(rep.leads_unlocked_per_decision, 0)

    def test_optimization_entry_is_draft(self):
        bs = [_mk("L1", "travel_commitment", "resolved_auto")]
        rep = M.compute(bs, [])
        entry = M.optimization_log_entry(rep, 1.0, ["evidence"])
        self.assertEqual(entry["verdict"], "inconclusive")
        self.assertIn("true_applicant_decisions", entry["post_metric"])


class ApplyHelpersTest(unittest.TestCase):
    def test_family_entry_format(self):
        s = A.family_entry("travel_commitment", "default", "PROMPT", 3)
        self.assertTrue(s.startswith("FAMILY[travel_commitment/default]:"))
        self.assertIn("[3 applications]", s)

    def test_qn_with_notes_string_field(self):
        rec = {"queue_notes": "existing"}
        self.assertEqual(A.qn_with_notes(rec, ["n1"]), "existing | n1")

    def test_qn_with_notes_list_field_preserved(self):
        rec = {"queue_notes": ["a"]}
        self.assertEqual(A.qn_with_notes(rec, ["b"]), ["a", "b"])

    def test_blocker_raws_skips_bookkeeping(self):
        rec = {"unresolved": ["real blocker text here",
                              "RESOLVED: old one",
                              "FAMILY[x/y]: prompt [2 applications]"]}
        self.assertEqual(A.blocker_raws(rec), ["real blocker text here"])

    def test_portable_families_constant(self):
        self.assertEqual(A.PORTABLE_APPLICANT_FAMILIES,
                         {"travel_commitment", "office_frequency",
                          "interview_recording_consent"})

    def test_tray_key_stable(self):
        self.assertEqual(A._tray_key("R1", "Text"), A._tray_key("R1", "Text"))
        self.assertNotEqual(A._tray_key("R1", "Text"), A._tray_key("R2", "Text"))


class DryRunFixturesTest(unittest.TestCase):
    def test_no_real_personal_data(self):
        recs = R.verified_candidate_records()
        blob = json.dumps(recs)
        # Synthetic stand-ins: the guard scans for PII-shaped leaks without
        # embedding any real personal identifier in the published history.
        for bad in ("alex.applicant1", "Alex", "Applicant", "999", "Springfield",
                    "linkedin.com/in/alex-applicant-example", "example.net"):
            self.assertNotIn(bad, blob, f"fixture leaks: {bad}")
        self.assertEqual(recs["email"][0], "applicant@example.com")
        self.assertIn("example candidate record", recs["email"][1])

    def test_needs_input_statuses_generic(self):
        self.assertNotIn("PACKET-READY-NEEDS-TRENT", R.NEEDS_INPUT_STATUSES)
        self.assertIn("PARKED-NEEDS-INPUT", R.NEEDS_INPUT_STATUSES)


class RecoverWatermarkTest(unittest.TestCase):
    def _wm(self, keys):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "wm.json")
        with open(p, "w") as f:
            json.dump(keys, f)
        return p

    def test_dry_run_drops_nothing_but_reports(self):
        p = self._wm(["k1", "k2", "orphan"])
        report, kept = RW.repair_watermark(p, ["k1", "k2"], dry_run=True)
        self.assertEqual(report["kept"], 2)
        self.assertEqual(report["dropped"], 1)
        self.assertEqual(json.load(open(p)), ["k1", "k2", "orphan"])

    def test_live_repair_backups_and_writes(self):
        p = self._wm(["k1", "orphan"])
        report, kept = RW.repair_watermark(p, ["k1"], dry_run=False)
        self.assertEqual(kept, ["k1"])
        self.assertEqual(json.load(open(p)), ["k1"])
        self.assertTrue(os.path.exists(report["backup"]))


class LearningLeakAuditTest(unittest.TestCase):
    def _files(self):
        d = tempfile.mkdtemp()
        ledger = os.path.join(d, "ledger.json")
        events = os.path.join(d, "events.jsonl")
        dec = os.path.join(d, "decisions.json")
        json.dump([
            {"role_id": "R1", "status": "SUBMITTED", "date_submitted": "2026-09-16"},
            {"role_id": "R2", "status": "SUBMITTED", "date_submitted": "2026-09-16"},
        ], open(ledger, "w"))
        with open(events, "w") as f:
            f.write(json.dumps({"event_type": "submitted", "role_id": "R1"}) + "\n")
        json.dump([{"role_id": "R1", "claim": "verified"}], open(dec, "w"))
        return ledger, events, dec

    def test_capture_math(self):
        ledger, events, dec = self._files()
        rep = LLA.audit(ledger_path=ledger, events_path=events,
                        decisions_path=dec)
        self.assertEqual(rep["submitted_rows"], 2)
        self.assertEqual(rep["captured"], 1)
        self.assertEqual(rep["capture_rate"], 0.5)
        self.assertTrue(rep["below_floor"])
        self.assertEqual(rep["leak_rows"], ["R2"])

    def test_empty_pool_is_full_capture(self):
        d = tempfile.mkdtemp()
        ledger = os.path.join(d, "l.json")
        json.dump([], open(ledger, "w"))
        rep = LLA.audit(ledger_path=ledger,
                        events_path=os.path.join(d, "no.jsonl"),
                        decisions_path=os.path.join(d, "no2.json"))
        self.assertEqual(rep["capture_rate"], 1.0)
        self.assertFalse(rep["below_floor"])

    def test_no_technique_library_reference(self):
        src = open(LLA.__file__, encoding="utf-8").read()
        self.assertNotIn("technique_library", src)


class PreferencesContractTest(unittest.TestCase):
    def test_applicant_families_exposed(self):
        self.assertIn("travel.unspecified_required", P.APPLICANT_ONLY_FAMILIES)
        self.assertFalse(hasattr(P, "TRENT_ONLY_FAMILIES"))


if __name__ == "__main__":
    unittest.main()
