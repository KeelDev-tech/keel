"""Regression tests for keel/tools/strategic_reconcile.py.

Pins the disposition classifier on fixtures so the reconciliation logic
cannot silently drift. All pure functions; no queue/ledger/manifest reads.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tools"))
import strategic_reconcile as sr


def entry(status, role_id="R-1"):
    return {"role_id": role_id, "status": status, "company": "C", "title": "T"}


def ledger_row(status, confirmation="confirmed ok"):
    return {"status": status, "confirmation_text": confirmation,
            "confirmation_url": ""}


class ClassifyEntryTests(unittest.TestCase):
    def test_submitted_evidence_ok(self):
        d, _ = sr.classify_entry(entry("SUBMITTED"), "READY", "ok",
                                 [ledger_row("SUBMITTED")])
        self.assertEqual(d, "submitted_evidence_ok")

    def test_submitted_zero_ledger_rows(self):
        d, detail = sr.classify_entry(entry("SUBMITTED"), "READY", "ok", [])
        self.assertEqual(d, "submitted_needs_ledger")
        self.assertIn("zero ledger rows", detail)

    def test_submitted_duplicate_rows(self):
        d, _ = sr.classify_entry(
            entry("SUBMITTED"), "READY", "ok",
            [ledger_row("NEEDS_INPUT", ""), ledger_row("SUBMITTED")])
        self.assertEqual(d, "submitted_duplicate_rows")

    def test_submitted_stale_outcome_rejected(self):
        d, _ = sr.classify_entry(entry("SUBMITTED"), "READY", "ok",
                                 [ledger_row("REJECTED", "")])
        self.assertEqual(d, "submitted_stale_outcome")

    def test_submitted_stale_outcome_interview(self):
        d, _ = sr.classify_entry(entry("SUBMITTED"), "READY", "ok",
                                 [ledger_row("INTERVIEW_INVITED", "")])
        self.assertEqual(d, "submitted_stale_outcome")

    def test_trapped_ready_parked(self):
        d, _ = sr.classify_entry(entry("PARKED"), "READY", "ok", [])
        self.assertEqual(d, "trapped_ready_reopen")

    def test_trapped_ready_skipped(self):
        d, _ = sr.classify_entry(entry("SKIPPED"), "READY_TO_SUBMIT", "ok", [])
        self.assertEqual(d, "trapped_ready_reopen")

    def test_trapped_ready_closed(self):
        d, _ = sr.classify_entry(entry("CLOSED"), "READY", "ok", [])
        self.assertEqual(d, "trapped_ready_closed")

    def test_trapped_ready_closed_expired(self):
        d, _ = sr.classify_entry(entry("CLOSED-EXPIRED"), "READY", "ok", [])
        self.assertEqual(d, "trapped_ready_closed")

    def test_trapped_ready_needs_input(self):
        d, _ = sr.classify_entry(
            entry("NEEDS_INPUT"), "READY_TO_SUBMIT (work authorization)", "ok",
            [])
        self.assertEqual(d, "trapped_ready_needs_input")

    def test_trapped_ready_gated_reconciles_gate_first(self):
        d, detail = sr.classify_entry(entry("GATED"), "READY", "ok", [])
        self.assertEqual(d, "trapped_ready_gated")
        self.assertIn("reconcile", detail)

    def test_manifest_ready_substring_matches(self):
        # "READY_TO_SUBMIT (work authorization + ...)" must count as ready
        self.assertTrue(sr.is_manifest_ready(
            "READY_TO_SUBMIT (work authorization + phone verified)"))

    def test_manifest_needs_input_not_ready(self):
        self.assertFalse(sr.is_manifest_ready("NEEDS_INPUT"))

    def test_gated_deferred_to_condition_classifier(self):
        d, _ = sr.classify_entry(entry("GATED"), None, "missing", [])
        self.assertEqual(d, "gated_pending_condition")

    def test_consistent_gated_manifest(self):
        d, _ = sr.classify_entry(entry("GATED"), "GATED", "ok", [])
        self.assertEqual(d, "gated_pending_condition")

    def test_missing_manifest(self):
        d, _ = sr.classify_entry(entry("UNVERIFIED"), None, "missing", [])
        self.assertEqual(d, "manifest_unparsed")


class GatedConditionClassTests(unittest.TestCase):
    def test_agent_verifiable_stale_crawl(self):
        self.assertEqual(
            sr.gated_condition_class(
                "Live status - stale crawl; re-confirm before any effort"),
            "agent_verifiable")

    def test_agent_verifiable_page_unopened(self):
        self.assertEqual(
            sr.gated_condition_class(
                "Live page not opened - confirm full JD, comp, and live status"),
            "agent_verifiable")

    def test_trent_only_attestation(self):
        self.assertEqual(
            sr.gated_condition_class(
                "physical-capacity and work-hours attestations"),
            "trent_only")

    def test_trent_only_tenure_framing(self):
        self.assertEqual(
            sr.gated_condition_class(
                "do not submit until Trent resolves the years-of-experience framing"),
            "trent_only")

    def test_trent_only_certificate_not_held(self):
        self.assertEqual(
            sr.gated_condition_class(
                "Responsible Beverage Service (RBS) Certificate required - not held"),
            "trent_only")

    def test_trent_only_availability(self):
        self.assertEqual(
            sr.gated_condition_class(
                "Able to work a flexible schedule including weekends - availability"),
            "trent_only")

    def test_unknown_conservative(self):
        # Ambiguous text must NOT be labeled agent-verifiable: a false
        # agent label would route a Trent-only blocker to an agent path.
        self.assertEqual(sr.gated_condition_class("PRE-APPLICATION REQUIREMENT"),
                         "unknown")
        self.assertEqual(sr.gated_condition_class("MANIFEST_UNRESOLVED"),
                         "unknown")
        self.assertEqual(sr.gated_condition_class(""), "unknown")

    def test_submitted_evidence_requires_canonical_fields(self):
        self.assertTrue(sr.submitted_evidence(
            {"confirmation_text": "Your application has been sent.",
             "confirmation_url": ""}))
        self.assertTrue(sr.submitted_evidence(
            {"confirmation_text": "",
             "confirmation_url": "https://ats.example/confirm/123"}))
        self.assertFalse(sr.submitted_evidence(
            {"confirmation_text": "", "confirmation_url": ""}))


    def test_gated_class_heading_derives_count_from_input(self):
        # The report heading must reflect the actual distinct GATED classes,
        # not a hard-coded historical count.
        def row(rid, cls):
            return {"role_id": rid, "company": "C", "title": "T", "fit_score": 80,
                    "queue_status": "GATED", "manifest_status": "ok",
                    "manifest_provenance": "p", "ledger_statuses": [],
                    "disposition": "gated_pending_condition",
                    "gate_condition_class": cls, "gate_condition": "cond",
                    "detail": "d"}
        rows = [row("R-1", "class-a"), row("R-2", "class-a"), row("R-3", "class-b")]
        summary = {"dispositions": {"gated_pending_condition": 3},
                   "gated_classes": {"class-a": 2, "class-b": 1}, "n": 3}
        report = sr.render_report(rows, summary)
        self.assertIn("## GATED condition classes (2)", report)
        self.assertNotIn("(70)", "\n".join(report))

    def test_gated_class_heading_zero_classes(self):
        summary = {"dispositions": {}, "gated_classes": {}, "n": 0}
        report = sr.render_report([], summary)
        self.assertIn("## GATED condition classes (0)", report)


if __name__ == "__main__":
    unittest.main()


# --- dry-run migration manifest (P8, 2026-09-19) ------------------------------

def _rec(role_id, disposition, detail="d"):
    return {"role_id": role_id, "disposition": disposition, "detail": detail,
            "queue_status": "PARKED", "manifest_status": "READY",
            "manifest_provenance": "manifest", "ledger_statuses": [],
            "ledger_rows": 0}


class MigrationManifestTests(unittest.TestCase):
    def test_every_row_accounted_including_no_move(self):
        entries = [entry("PARKED", "R-1"), entry("READY", "R-2")]
        recs = [_rec("R-1", "trapped_ready_reopen"),
                _rec("R-2", "consistent")]
        m = sr.build_migration_manifest(entries, recs, "abc123")
        self.assertEqual(m["rows_accounted"], 2)
        self.assertEqual(len(m["items"]), 2)
        by_role = {i["role_id"]: i for i in m["items"]}
        self.assertIsNotNone(by_role["R-1"]["proposed_destination"])
        self.assertIsNone(by_role["R-2"]["proposed_destination"])
        self.assertEqual(m["moves_proposed"], 1)

    def test_destinations_cover_actionable_dispositions(self):
        cases = {"trapped_ready_reopen": "READY",
                 "trapped_ready_needs_input": "NEEDS_INPUT",
                 "trapped_ready_gated": "GATED",
                 "trapped_ready_closed": "REJECTED",
                 "submitted_stale_outcome": "sync"}
        for disp, needle in cases.items():
            m = sr.build_migration_manifest(
                [entry("X", "R-1")], [_rec("R-1", disp)], "abc123")
            dest = m["items"][0]["proposed_destination"]
            self.assertIn(needle, dest, disp)

    def test_original_row_retained_and_digested(self):
        e = entry("PARKED", "R-9")
        m = sr.build_migration_manifest([e], [_rec("R-9", "consistent")], "abc123")
        item = m["items"][0]
        self.assertEqual(item["original_row"], e)
        import hashlib, json
        expect = hashlib.sha256(
            json.dumps(e, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(item["original_row_digest"], expect)

    def test_idempotent_items_and_dry_run_authority(self):
        entries = [entry("PARKED", "R-1"), entry("GATED", "R-2")]
        recs = [_rec("R-1", "trapped_ready_reopen"), _rec("R-2", "gated_pending_condition")]
        m1 = sr.build_migration_manifest(entries, recs, "abc123")
        m2 = sr.build_migration_manifest(entries, recs, "abc123")
        self.assertEqual(m1["items"], m2["items"])
        self.assertEqual(m1["expected_source_revision"], "abc123")
        for i in m1["items"]:
            self.assertIn("dry-run", i["authority"])
        self.assertIn("dry-run", m1["authority"])
        self.assertTrue(all(i["migration_id"].startswith("mig-") for i in m1["items"]))

    def test_manifest_is_fresh_rejects_stale_queue(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('[{"role_id": "R-1", "status": "PARKED"}]')
            path = f.name
        try:
            digest = sr.queue_revision(path)
            m = sr.build_migration_manifest(
                [{"role_id": "R-1"}], [_rec("R-1", "consistent")], digest)
            self.assertTrue(sr.manifest_is_fresh(m, path))
            with open(path, "w") as f:
                f.write('[{"role_id": "R-1", "status": "READY"}]')
            self.assertFalse(sr.manifest_is_fresh(m, path))
        finally:
            os.unlink(path)
