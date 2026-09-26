"""Option B (2026-09-17, ARM 3) regression tests: the sticky
never_auto_submit_attestation marker.

The marker kills the verify->READY->prescreen-park pendulum at the
promotion gate: a lead parked on resolver-abstained attestation keys
(e.g. personally_completed_certification) can never be auto-submitted,
so verify_retry must never promote it READY while the keys stay
unanswerable. The marker lifts itself once the bank resolves the keys
(the applicant's words), and fails closed when the resolver/bank is unreadable.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines"))  # engines are not on sys.path; tests live in tests/

import answer_resolver
import apply_loop
import prescreen
import verify_retry as vr

KEY = "personally_completed_certification"


def _entry(**kw):
    e = {"role_id": "ATS8-GREENHOUSE-STRIPE-TEST-1", "company": "Stripe",
         "title": "Test", "status": "PARKED-PENDING-VERIFICATION",
         "fit_score": 80, "action_band": "APPLY"}
    e.update(kw)
    return e


def _resolution(status):
    return answer_resolver.Resolution(key=KEY, status=status,
                                      value="" if status == "abstain" else "x")


class MarkerExtractionTest(unittest.TestCase):
    """apply_loop._never_auto_submit_keys picks only abstained attestation
    mismatches -- no thresholds, no policy changes."""

    def test_abstained_attestation_extracted(self):
        gr = {"mismatches": [
            {"key": KEY, "category": "attestation", "kind": "out_of_scope",
             "bank": "", "packet": ""},
            {"key": "arbitration_agreement", "category": "attestation",
             "kind": "value_drift", "bank": "y", "packet": "n"},
            {"key": "email", "category": "contact", "kind": "out_of_scope",
             "bank": "", "packet": ""},
        ]}
        self.assertEqual(apply_loop._never_auto_submit_keys(gr), [KEY])

    def test_empty_or_none_safe(self):
        self.assertEqual(apply_loop._never_auto_submit_keys({}), [])
        self.assertEqual(apply_loop._never_auto_submit_keys(None), [])
        self.assertEqual(
            apply_loop._never_auto_submit_keys({"mismatches": None}), [])


class ParkLeadMarkerTest(unittest.TestCase):
    def setUp(self):
        self._log_patcher = mock.patch.object(prescreen, "log_event")
        self._log_patcher.start()

    def tearDown(self):
        self._log_patcher.stop()

    def _qdir_with_lead(self, qdir, **lead_kw):
        lead = {"role_id": "STRIPE-TEST-1", "company": "Stripe",
                "title": "T", "status": "READY", "status_reason": "",
                "unresolved": []}
        lead.update(lead_kw)
        json.dump([lead], open(os.path.join(qdir, "standard-queue.json"), "w"))
        json.dump([], open(os.path.join(qdir, "needs_input-queue.json"), "w"))

    def test_marker_stamped_when_keys_given(self):
        with tempfile.TemporaryDirectory() as qdir:
            self._qdir_with_lead(qdir)
            out = prescreen.park_lead(
                "STRIPE-TEST-1", ["attestation blocker needs the applicant"],
                queue_dir=qdir, backup=False,
                never_auto_submit_keys=[KEY])
            self.assertTrue(out["ok"], out)
            ni = json.load(open(os.path.join(qdir, "needs_input-queue.json")))
            self.assertEqual(ni[0]["never_auto_submit_attestation"], [KEY])

    def test_no_marker_without_keys(self):
        with tempfile.TemporaryDirectory() as qdir:
            self._qdir_with_lead(qdir)
            out = prescreen.park_lead(
                "STRIPE-TEST-1", ["travel blocker needs the applicant"],
                queue_dir=qdir, backup=False)
            self.assertTrue(out["ok"], out)
            ni = json.load(open(os.path.join(qdir, "needs_input-queue.json")))
            self.assertNotIn("never_auto_submit_attestation", ni[0])

    def test_marker_deduped_and_sorted(self):
        with tempfile.TemporaryDirectory() as qdir:
            self._qdir_with_lead(qdir)
            out = prescreen.park_lead(
                "STRIPE-TEST-1", ["attestation blocker needs the applicant"],
                queue_dir=qdir, backup=False,
                never_auto_submit_keys=["b_key", KEY, "b_key", ""])
            self.assertTrue(out["ok"], out)
            ni = json.load(open(os.path.join(qdir, "needs_input-queue.json")))
            self.assertEqual(ni[0]["never_auto_submit_attestation"],
                             ["b_key", KEY])


class PromotionGateTest(unittest.TestCase):
    """verify_retry.apply_live_entry honors the marker."""

    def _resolve_side(self, status_map):
        def _side(key, entry, employer=None, role_context=None):
            return answer_resolver.Resolution(
                key=key,
                status=status_map.get(key, answer_resolver.STATUS_ABSTAIN))
        return _side

    def _patched(self, status_map, fail_load=False):
        load = (mock.patch("answer_resolver.load_bank",
                           side_effect=Exception("bank gone"))
                if fail_load else
                mock.patch("answer_resolver.load_bank",
                           return_value={"answers": {KEY: {"answer": "DO NOT CERTIFY"}}}))
        res = mock.patch("answer_resolver.resolve",
                         side_effect=self._resolve_side(status_map))
        return load, res

    def test_marked_lead_never_promotes_not_held(self):
        load, res = self._patched({KEY: "abstain"})
        with load, res, mock.patch.object(vr, "materials_ready",
                                          return_value=(True, "")):
            e = _entry(never_auto_submit_attestation=[KEY])
            action, note = vr.apply_live_entry(
                e, was_held=False, promotable=True)
        self.assertEqual(action, "noop")
        self.assertIn(KEY, note)
        self.assertIn("never-auto-submit", note)
        # Marker is sticky: the blocked decision does NOT strip it.
        self.assertEqual(e["never_auto_submit_attestation"], [KEY])

    def test_marked_lead_never_releases_when_held(self):
        load, res = self._patched({KEY: "abstain"})
        with load, res, mock.patch.object(vr, "materials_ready",
                                          return_value=(True, "")):
            e = _entry(status=vr.HOLD_STATUS,
                       never_auto_submit_attestation=[KEY])
            action, note = vr.apply_live_entry(
                e, was_held=True, promotable=True)
        self.assertEqual(action, "stay")
        self.assertIn(KEY, note)

    def test_unmarked_lead_still_promotes(self):
        # Control: no marker, clean lead -> promotion path untouched.
        load, res = self._patched({KEY: "abstain"})
        with load, res, mock.patch.object(vr, "materials_ready",
                                          return_value=(True, "")):
            action, _n = vr.apply_live_entry(
                _entry(), was_held=False, promotable=True)
        self.assertEqual(action, "promote")

    def test_marker_lifts_when_bank_resolves(self):
        # the applicant banked an employer-scoped answer since the park: the key
        # resolves, the marker pops, promotion proceeds.
        load, res = self._patched({KEY: "resolved"})
        with load, res, mock.patch.object(vr, "materials_ready",
                                          return_value=(True, "")):
            e = _entry(never_auto_submit_attestation=[KEY])
            action, _n = vr.apply_live_entry(
                e, was_held=False, promotable=True)
        self.assertEqual(action, "promote")
        self.assertNotIn(vr.ATTN_MARKER_FIELD, e)

    def test_partial_block_stays_blocked(self):
        other = "no_ai_interviewer_policy_acme"
        load, res = self._patched({KEY: "resolved", other: "abstain"})
        with load, res, mock.patch.object(vr, "materials_ready",
                                          return_value=(True, "")):
            e = _entry(never_auto_submit_attestation=[KEY, other])
            action, note = vr.apply_live_entry(
                e, was_held=False, promotable=True)
        self.assertEqual(action, "noop")
        self.assertIn(other, note)
        self.assertNotIn(KEY, note)
        self.assertEqual(e["never_auto_submit_attestation"], [KEY, other])

    def test_fail_closed_when_bank_unreadable(self):
        load, res = self._patched({KEY: "abstain"}, fail_load=True)
        with load, res, mock.patch.object(vr, "materials_ready",
                                          return_value=(True, "")):
            action, note = vr.apply_live_entry(
                _entry(never_auto_submit_attestation=[KEY]),
                was_held=False, promotable=True)
        self.assertEqual(action, "noop")
        self.assertIn("withheld", note)

    def test_marker_check_does_not_block_nonpromotable(self):
        # Marker only gates promotion; a non-promotable lead keeps its
        # existing noop path (no behavior change, no crash).
        load, res = self._patched({KEY: "abstain"})
        with load, res:
            action, _n = vr.apply_live_entry(
                _entry(never_auto_submit_attestation=[KEY]),
                was_held=False, promotable=False)
        self.assertEqual(action, "noop")


if __name__ == "__main__":
    unittest.main()
