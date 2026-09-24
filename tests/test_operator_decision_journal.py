#!/usr/bin/env python3
"""Regression tests for engines/operator_decision_journal.py.

Ports the 0.16 operator journal guarantees (immutable events, MAC chain,
CAS head, monotonic clock, revocation/expiry, offline-restore fencing)
to our approval_records model. Each test fails on the defect it guards:
a journal that silently edits history, accepts a stale head, or mints
from an unauthenticated origin must not pass.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engines"))
from operator_decision_journal import DecisionJournal, JournalError, DB_NAME

BASE = int(datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc).timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def make_journal(now=BASE):
    tmp = tempfile.mkdtemp(prefix="odj-test-")
    home = os.path.join(tmp, "journal")
    clock = [now]
    j = DecisionJournal.create(home, clock=lambda: clock[0])
    return j, home, clock


def ev(text="operator says go", origin="operator_channel", ref="test"):
    return {"origin": origin, "text": text, "ref": ref}


def decide(j, head, did="dec-1", scope=None, clock_now=None, **kw):
    params = dict(decision_id=did, decision="approve",
                  scope=scope or {"action": "test_action"},
                  evidence=ev(), ttl_h=24,
                  expected_head_sha256=head, confirmed=True)
    params.update(kw)
    return j.decide(**params)


class JournalTest(unittest.TestCase):
    def test_create_empty_verify_ok(self):
        j, home, _ = make_journal()
        view = j.inspect()
        self.assertEqual(view["decisions"], {})
        self.assertEqual(view["event_count"], 1)  # genesis only
        ok, checks = j.verify()
        self.assertTrue(ok, checks)

    def test_decide_approve_and_export(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head)
        self.assertEqual(out["state"], "APPROVED")
        view = j.inspect()
        self.assertEqual(view["decisions"]["dec-1"]["state"], "APPROVED")
        self.assertEqual(view["decisions"]["dec-1"]["principal"],
                         "operator (operator channel)")
        export = j.export()
        self.assertEqual(len(export["grants"]), 1)
        self.assertEqual(export["head_sha256"], out["head_sha256"])
        ok, _ = j.verify()
        self.assertTrue(ok)

    def test_reject_not_exported(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head, decision="reject")
        self.assertEqual(out["state"], "REJECTED")
        self.assertEqual(j.export()["grants"], [])

    def test_cas_mismatch_fails_closed(self):
        j, home, _ = make_journal()
        with self.assertRaises(JournalError) as cm:
            decide(j, "0" * 64)
        self.assertEqual(str(cm.exception), "decision_head_compare_and_swap_failed")
        # concurrent writer interleave: first decide moves head; stale head fails
        head = j.inspect()["head_sha256"]
        decide(j, head, did="dec-a")
        with self.assertRaises(JournalError):
            decide(j, head, did="dec-b")

    def test_revoke_and_export_excludes(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head)
        rev = j.revoke(decision_id="dec-1", reason="operator changed mind",
                       evidence=ev("revoking"), expected_head_sha256=out["head_sha256"],
                       confirmed=True)
        self.assertEqual(rev["state"], "REVOKED")
        view = j.inspect()
        self.assertEqual(view["decisions"]["dec-1"]["state"], "REVOKED")
        self.assertEqual(j.export()["grants"], [])
        matched, _ = j.grants_cover({"action": "test_action"})
        self.assertEqual(matched, [])

    def test_revoke_requires_current_approval(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head, decision="reject")
        with self.assertRaises(JournalError) as cm:
            j.revoke(decision_id="dec-1", reason="x", evidence=ev(),
                     expected_head_sha256=out["head_sha256"], confirmed=True)
        self.assertEqual(str(cm.exception), "revocation_requires_current_approval")

    def test_expiry(self):
        j, home, clock = make_journal(now=BASE)
        head = j.inspect()["head_sha256"]
        decide(j, head, ttl_h=1)  # expires BASE+3600
        clock[0] = BASE + 7200
        j2 = DecisionJournal(home, clock=lambda: clock[0])
        self.assertEqual(j2.inspect()["decisions"]["dec-1"]["state"], "EXPIRED")
        self.assertEqual(j2.export()["grants"], [])

    def test_clock_rollback_rejected(self):
        j, home, clock = make_journal(now=BASE + 1000)
        head = j.inspect()["head_sha256"]
        decide(j, head)
        clock[0] = BASE  # rewind
        with self.assertRaises(JournalError):
            DecisionJournal(home, clock=lambda: clock[0])

    def test_expiry_required(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            decide(j, head, ttl_h=None)
        self.assertEqual(str(cm.exception), "decision_expiry_required")

    def test_unauthenticated_origin_rejected(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            decide(j, head, evidence=ev(origin="worker_claim"))
        self.assertEqual(str(cm.exception), "decision_origin_not_authenticated")

    def test_confirmation_required(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            j.decide(decision_id="dec-1", decision="approve",
                     scope={"action": "x"}, evidence=ev(), ttl_h=1,
                     expected_head_sha256=head, confirmed=False)
        self.assertEqual(str(cm.exception), "explicit_decision_confirmation_required")

    def test_duplicate_decision_id_rejected(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head)
        with self.assertRaises(JournalError) as cm:
            decide(j, out["head_sha256"], did="dec-1")
        self.assertEqual(str(cm.exception), "decision_id_already_recorded")

    def test_supersede(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head, did="dec-old")
        out2 = decide(j, out["head_sha256"], did="dec-new", supersedes="dec-old")
        view = j.inspect()
        self.assertEqual(view["decisions"]["dec-old"]["state"], "SUPERSEDED")
        self.assertEqual(view["decisions"]["dec-new"]["state"], "APPROVED")
        self.assertEqual(len(j.export()["grants"]), 1)

    def test_supersede_requires_approved_target(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            decide(j, head, did="dec-new", supersedes="dec-missing")
        self.assertEqual(str(cm.exception), "supersede_target_not_currently_approved")

    def test_sql_update_blocked_by_trigger(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        decide(j, head)
        db = sqlite3.connect(os.path.join(home, DB_NAME))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE events SET payload='{}' WHERE sequence=1")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM events WHERE sequence=1")
        db.close()
        ok, _ = j.verify()
        self.assertTrue(ok)

    def test_key_tamper_breaks_verify(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        decide(j, head)
        # attacker swaps the MAC key: chain must no longer verify
        with open(os.path.join(home, "decision-journal.key"), "wb") as f:
            f.write(os.urandom(32))
        ok, checks = j.verify()
        self.assertFalse(ok)
        self.assertTrue(any("integrity" in c[0] for c in checks))

    def test_offline_restore_fences_open(self):
        j, home, _ = make_journal()
        marker = os.path.join(os.path.dirname(home), "OFFLINE_RESTORE.json")
        with open(marker, "w") as f:
            f.write("{}")
        with self.assertRaises(JournalError) as cm:
            DecisionJournal(home)
        self.assertEqual(str(cm.exception),
                         "restored_journal_requires_reviewed_rebinding")

    def test_note_does_not_authorize(self):
        j, home, _ = make_journal()
        j.note(text="context annotation", ref="test")
        self.assertEqual(j.inspect()["decisions"], {})
        self.assertEqual(j.export()["grants"], [])
        ok, _ = j.verify()
        self.assertTrue(ok)

    def test_grants_cover_wildcard(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        decide(j, head, scope={"action": "launch", "board": "*"})
        matched, pin = j.grants_cover({"action": "launch", "board": "ashby"})
        self.assertEqual(len(matched), 1)
        self.assertEqual(pin, j.export()["approvals_sha256"])
        matched, _ = j.grants_cover({"action": "launch", "board": "ashby",
                                     "extra": "nope"})
        self.assertEqual(matched, [])

    def test_backfilled_flag_preserved(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        decide(j, head, backfilled=True,
               evidence={"origin": "operator_channel",
                         "text": "earlier words", "at": iso(BASE - 3600),
                         "ref": "transcript"})
        s = j.inspect()["decisions"]["dec-1"]
        self.assertTrue(s["backfilled"])
        self.assertEqual(s["evidence"]["at"], iso(BASE - 3600))

    def test_double_create_rejected(self):
        j, home, _ = make_journal()
        with self.assertRaises(JournalError) as cm:
            DecisionJournal.create(home)
        self.assertEqual(str(cm.exception), "journal_already_exists")


class BatchAppendTest(unittest.TestCase):
    """append_batch: O(N) bulk path with a hard bulk guard (fix #12)."""

    def test_batch_appends_notes_verify_ok(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = j.append_batch(
            items=[{"kind": "note", "text": f"note {i}", "ref": "test"}
                   for i in range(5)],
            expected_head_sha256=head, confirmed=True)
        self.assertEqual(out["appended"], 5)
        self.assertEqual(out["head_sha256"], j.inspect()["head_sha256"])
        self.assertEqual(j.inspect()["event_count"], 6)  # genesis + 5
        # notes authorize nothing
        self.assertEqual(j.inspect()["decisions"], {})
        self.assertEqual(j.export()["grants"], [])
        ok, checks = j.verify()
        self.assertTrue(ok, checks)

    def test_batch_too_large_refused_journal_unchanged(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            j.append_batch(
                items=[{"kind": "note", "text": f"note {i}"} for i in range(51)],
                expected_head_sha256=head, confirmed=True)
        self.assertEqual(str(cm.exception), "bulk_append_too_large")
        self.assertEqual(j.inspect()["event_count"], 1)

    def test_batch_cap_boundary(self):
        from operator_decision_journal import APPEND_BATCH_MAX
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = j.append_batch(
            items=[{"kind": "note", "text": f"note {i}"}
                   for i in range(APPEND_BATCH_MAX)],
            expected_head_sha256=head, confirmed=True)
        self.assertEqual(out["appended"], APPEND_BATCH_MAX)
        ok, _ = j.verify()
        self.assertTrue(ok)

    def test_batch_requires_confirmation(self):
        j, home, _ = make_journal()
        with self.assertRaises(JournalError) as cm:
            j.append_batch(items=[{"kind": "note", "text": "x"}],
                           expected_head_sha256="0" * 64, confirmed=False)
        self.assertEqual(str(cm.exception), "explicit_batch_confirmation_required")

    def test_batch_empty_refused(self):
        j, home, _ = make_journal()
        with self.assertRaises(JournalError) as cm:
            j.append_batch(items=[], expected_head_sha256="0" * 64,
                           confirmed=True)
        self.assertEqual(str(cm.exception), "batch_items_invalid")

    def test_batch_rejects_non_note_kind(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            j.append_batch(
                items=[{"kind": "decide", "text": "sneaky"}],
                expected_head_sha256=head, confirmed=True)
        self.assertEqual(str(cm.exception), "batch_kind_not_note")
        self.assertEqual(j.inspect()["event_count"], 1)

    def test_batch_rejects_bad_note_text(self):
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        with self.assertRaises(JournalError) as cm:
            j.append_batch(items=[{"kind": "note", "text": ""}],
                           expected_head_sha256=head, confirmed=True)
        self.assertEqual(str(cm.exception), "note_text_invalid")
        self.assertEqual(j.inspect()["event_count"], 1)

    def test_batch_cas_mismatch_fails_closed(self):
        j, home, _ = make_journal()
        with self.assertRaises(JournalError) as cm:
            j.append_batch(items=[{"kind": "note", "text": "x"}],
                           expected_head_sha256="0" * 64, confirmed=True)
        self.assertEqual(str(cm.exception),
                         "batch_head_compare_and_swap_failed")
        self.assertEqual(j.inspect()["event_count"], 1)

    def test_batch_head_pin_required(self):
        j, home, _ = make_journal()
        with self.assertRaises(JournalError) as cm:
            j.append_batch(items=[{"kind": "note", "text": "x"}],
                           expected_head_sha256="short", confirmed=True)
        self.assertEqual(str(cm.exception), "batch_head_pin_required")

    def test_batch_is_single_load_not_per_event(self):
        """Structural pin for the O(n^2) fix: one _load per batch, not one
        per note. 1-note and 50-note batches must trigger the same number of
        _load calls."""
        from operator_decision_journal import DecisionJournal as DJ
        counts = []

        def counting_load(self, db, key):
            counts.append(1)
            return orig_load(self, db, key)

        orig_load = DJ._load
        DJ._load = counting_load
        try:
            j, home, _ = make_journal()
            head = j.inspect()["head_sha256"]
            n_before = len(counts)
            j.append_batch(items=[{"kind": "note", "text": "one"}],
                           expected_head_sha256=head, confirmed=True)
            loads_one = len(counts) - n_before
            head = j.inspect()["head_sha256"]
            n_before = len(counts)
            j.append_batch(
                items=[{"kind": "note", "text": f"n{i}"} for i in range(50)],
                expected_head_sha256=head, confirmed=True)
            loads_fifty = len(counts) - n_before
            self.assertEqual(loads_one, loads_fifty,
                             "batch cost must not grow per-note")
            # exactly: write-txn open + pre-commit re-verify
            self.assertEqual(loads_one, 2)
        finally:
            DJ._load = orig_load

    def test_single_append_paths_unaffected(self):
        """decide/revoke/note keep their own validation after the change."""
        j, home, _ = make_journal()
        head = j.inspect()["head_sha256"]
        out = decide(j, head)
        j.revoke(decision_id="dec-1", reason="changed mind", evidence=ev(),
                 expected_head_sha256=out["head_sha256"], confirmed=True)
        j.note(text="still works", ref="test")
        self.assertEqual(j.inspect()["event_count"], 4)
        ok, _ = j.verify()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
