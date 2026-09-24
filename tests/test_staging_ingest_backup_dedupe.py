"""test_staging_ingest_backup_dedupe.py — REPAIR B (2026-09-22, readmit-review).

Incident: staging_ingest rejected the re-verified Landed lead as "duplicate
employer+title in queue" — not from any live queue, but from 7
queue/_backup-* snapshot files holding the Sept-18 SWEEP17 entry. Legacy
load_queue_keys() globbed "*-queue.json", which matches _backup-* files;
DedupeIndex._queue_files() explicitly excludes underscore-prefixed backups.
The two paths disagreed, making re-admission structurally impossible.

Fix contract: load_queue_keys() skips underscore-prefixed files (aligned
with the index). Backup rows never block ingest; live-queue duplicates
still block.

unittest style (no pytest on this VM), run with:
    python3 -m unittest tests.test_staging_ingest_backup_dedupe
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))
import staging_ingest as si  # noqa: E402


def _lead(rid, company="Landed", title="Business Operations Manager"):
    return {"role_id": rid, "company": company, "title": title,
            "action_band": "APPLY", "status": "READY", "fit_score": 80,
            "application_url": "https://www.ycombinator.com/companies/"
                               "landed-2/jobs/Q7xQ1OT-business-operations-manager"}


class BackupDedupeFixture(unittest.TestCase):
    def setUp(self):
        self.qdir = tempfile.mkdtemp(prefix="si-backup-test-")
        # Live queue files.
        json.dump([], open(os.path.join(self.qdir, "standard-queue.json"), "w"))
        json.dump([], open(os.path.join(self.qdir, "rejected-queue.json"), "w"))

    def tearDown(self):
        shutil.rmtree(self.qdir, ignore_errors=True)

    def write_backup(self, name="_backup-20260918-test-queue.json", entries=None):
        entries = entries if entries is not None else [
            {"role_id": "SWEEP17-LANDED-BIZOPSMGR-20260918",
             "company": "Landed", "title": "Business Operations Manager",
             "status": "REJECTED"}]
        path = os.path.join(self.qdir, name)
        json.dump(entries, open(path, "w"))
        return path


class TestBackupDedupe(BackupDedupeFixture):
    def test_backup_rows_do_not_block_ingest(self):
        """The incident shape: duplicate key lives ONLY in _backup-* files."""
        self.write_backup()
        ids, keys = si.load_queue_keys(self.qdir)
        self.assertNotIn(("landed", "business operations manager"), keys,
                         "backup snapshot rows must not feed the dedupe keys")
        self.assertNotIn("SWEEP17-LANDED-BIZOPSMGR-20260918", ids)

    def test_live_queue_duplicate_still_blocks(self):
        """The fix must not weaken the live dedupe gate."""
        self.write_backup()
        json.dump([{"role_id": "LIVE-1", "company": "Landed",
                    "title": "Business Operations Manager", "status": "READY"}],
                  open(os.path.join(self.qdir, "standard-queue.json"), "w"))
        ids, keys = si.load_queue_keys(self.qdir)
        self.assertIn(("landed", "business operations manager"), keys)
        self.assertIn("LIVE-1", ids)

    def test_triage_admits_lead_when_only_backup_matches(self):
        """End-to-end at the triage layer: a re-admitted lead whose
        employer+title appears only in backups must not be rejected."""
        self.write_backup()
        queue_ids, queue_keys = si.load_queue_keys(self.qdir)
        ingested, rejected = si.triage([_lead("LANDED-BIZOPS-REMOTE-20260922")],
                                       queue_ids, queue_keys, set(),
                                       index=None)
        self.assertEqual([r for r, _ in rejected], [],
                         f"backup-only duplicate must not reject: {rejected}")
        self.assertEqual(len(ingested), 1)

    def test_triage_rejects_live_employer_title_duplicate(self):
        """Live-queue employer+title duplicates still reject."""
        json.dump([{"role_id": "LIVE-1", "company": "Landed",
                    "title": "Business Operations Manager", "status": "READY"}],
                  open(os.path.join(self.qdir, "standard-queue.json"), "w"))
        queue_ids, queue_keys = si.load_queue_keys(self.qdir)
        ingested, rejected = si.triage([_lead("LANDED-BIZOPS-REMOTE-20260922")],
                                       queue_ids, queue_keys, set(),
                                       index=None)
        self.assertEqual(len(ingested), 0)
        self.assertEqual([r for r, _ in rejected],
                         ["LANDED-BIZOPS-REMOTE-20260922"])


if __name__ == "__main__":
    unittest.main()
