"""Indexed ledger anchors detect replacement and restored history without scans."""
import copy
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from keel_efficiency.ledger import (
    CHECKPOINT_SCHEMA, ConflictError, LedgerError, MAX_INTEGER, ResourceLedger,
)


class LedgerCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "ledger.sqlite"
        self.ledger = ResourceLedger(self.path)
        self.ledger.create_scope("window", {"calls": 100})

    def backup(self):
        path = self.root / "backup.sqlite"
        with sqlite3.connect(self.path) as source, sqlite3.connect(path) as target:
            source.backup(target)
        return path

    def restore(self, path):
        self.path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        shutil.copyfile(path, self.path)

    def test_empty_checkpoint_is_stable_and_accepts_later_events(self):
        before = self.ledger.checkpoint()
        self.assertEqual(before["schema"], CHECKPOINT_SCHEMA)
        self.assertEqual(before["event_sequence"], 0)
        self.assertIsNone(before["event_sha256"])
        self.assertEqual(ResourceLedger(self.path).checkpoint(), before)
        self.ledger.reserve("request", "window", {"calls": 1})
        self.assertTrue(self.ledger.validate_checkpoint(before))

    def test_old_anchor_stays_valid_after_every_lifecycle_event(self):
        self.ledger.reserve("request", "window", {"calls": 2})
        checkpoint = self.ledger.checkpoint()
        self.assertEqual(checkpoint["event_sequence"], 1)
        self.ledger.mark_dispatched("request")
        self.ledger.mark_unknown("request", "result interrupted")
        self.ledger.settle("request", {"calls": 1})
        self.ledger.reconcile("request", {"input_tokens": 0, "output_tokens": 0,
                                           "compute_ms": 0, "external_credit_micros": 0})
        self.ledger.reserve("cancelled", "window", {"calls": 1})
        self.ledger.cancel("cancelled")
        self.assertTrue(self.ledger.validate_checkpoint(checkpoint))
        self.assertEqual(self.ledger.checkpoint()["event_sequence"], 7)

    def test_idempotent_lifecycle_replays_do_not_change_anchor(self):
        self.ledger.reserve("request", "window", {"calls": 1})
        self.ledger.mark_dispatched("request")
        self.ledger.settle("request", {"calls": 1})
        checkpoint = self.ledger.checkpoint()
        self.ledger.reserve("request", "window", {"calls": 1})
        self.ledger.settle("request", {"calls": 1})
        self.assertEqual(self.ledger.checkpoint(), checkpoint)

    def test_recreated_database_rejected_even_with_identical_requests(self):
        self.ledger.reserve("request", "window", {"calls": 1})
        checkpoint = self.ledger.checkpoint()
        self.path.unlink()
        replacement = ResourceLedger(self.path)
        replacement.create_scope("window", {"calls": 100})
        replacement.reserve("request", "window", {"calls": 1})
        with self.assertRaisesRegex(ConflictError, "instance differs"):
            replacement.validate_checkpoint(checkpoint)

    def test_missing_database_is_not_recreated_by_reads(self):
        checkpoint = self.ledger.checkpoint()
        self.path.unlink()
        for operation in (self.ledger.checkpoint,
                          lambda: self.ledger.validate_checkpoint(checkpoint)):
            with self.assertRaises(LedgerError):
                operation()
            self.assertFalse(self.path.exists())

    def test_restored_snapshot_before_anchor_is_rejected(self):
        backup = self.backup()
        self.ledger.reserve("request", "window", {"calls": 1})
        checkpoint = self.ledger.checkpoint()
        self.restore(backup)
        with self.assertRaisesRegex(ConflictError, "missing a checkpoint event"):
            self.ledger.validate_checkpoint(checkpoint)

    def test_restored_branch_reusing_sequence_and_fixed_clock_is_rejected(self):
        backup = self.backup()
        with patch("keel_efficiency.ledger.time.time_ns", return_value=123456789):
            self.ledger.reserve("request", "window", {"calls": 1})
            checkpoint = self.ledger.checkpoint()
            self.restore(backup)
            self.ledger.reserve("request", "window", {"calls": 1})
        replacement = self.ledger.checkpoint()
        self.assertEqual(checkpoint["instance_id"], replacement["instance_id"])
        self.assertEqual(checkpoint["event_sequence"], replacement["event_sequence"])
        self.assertNotEqual(checkpoint["event_sha256"], replacement["event_sha256"])
        with self.assertRaisesRegex(ConflictError, "differs from retained checkpoint event"):
            self.ledger.validate_checkpoint(checkpoint)

    def test_anchor_binds_complete_event_contents(self):
        self.ledger.reserve("request", "window", {"calls": 1})
        checkpoint = self.ledger.checkpoint()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE efficiency_events SET payload_json=? WHERE sequence=1", ('{"calls":2}',))
        with self.assertRaises(ConflictError):
            self.ledger.validate_checkpoint(checkpoint)

    def test_old_schema_migrates_without_rewriting_historical_events(self):
        legacy_path = self.root / "legacy.sqlite"
        with sqlite3.connect(legacy_path) as db:
            db.execute("CREATE TABLE efficiency_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL)")
            db.execute("INSERT INTO efficiency_meta VALUES(1,'keel.efficiency.ledger.v1')")
            db.execute("""CREATE TABLE efficiency_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                event TEXT NOT NULL, payload_json TEXT NOT NULL, created_ns INTEGER NOT NULL)""")
            db.execute("INSERT INTO efficiency_events(request_id,event,payload_json,created_ns) VALUES(?,?,?,?)",
                       ("old", "RESERVED", '{"calls":1}', 1))
            original = db.execute("SELECT * FROM efficiency_events").fetchone()
        migrated = ResourceLedger(legacy_path)
        checkpoint = migrated.checkpoint()
        with sqlite3.connect(legacy_path) as db:
            after = db.execute("SELECT * FROM efficiency_events").fetchone()
        self.assertEqual(after[:-1], original)
        self.assertIsNone(after[-1])
        self.assertEqual(checkpoint["event_sequence"], 1)
        self.assertTrue(migrated.validate_checkpoint(checkpoint))
        self.assertEqual(ResourceLedger(legacy_path).checkpoint(), checkpoint)
        migrated.create_scope("new", {"calls": 1})
        migrated.reserve("new", "new", {"calls": 1})
        self.assertTrue(migrated.validate_checkpoint(checkpoint))
        self.assertEqual(migrated.checkpoint()["event_sequence"], 2)

    def test_legacy_migration_preserves_requests_and_accounting(self):
        self.ledger.reserve("settled", "window", {"calls": 3})
        self.ledger.mark_dispatched("settled")
        self.ledger.settle("settled", {"calls": 2})
        self.ledger.reserve("pending", "window", {"calls": 5})
        before_scope = self.ledger.snapshot("window")
        before_requests = [self.ledger.request(name) for name in ("settled", "pending")]
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE efficiency_meta DROP COLUMN instance_id")
            db.execute("ALTER TABLE efficiency_events DROP COLUMN event_id")
            events = db.execute("SELECT * FROM efficiency_events ORDER BY sequence").fetchall()
        migrated = ResourceLedger(self.path)
        self.assertEqual(migrated.snapshot("window"), before_scope)
        self.assertEqual([migrated.request(name) for name in ("settled", "pending")], before_requests)
        with sqlite3.connect(self.path) as db:
            after = db.execute("SELECT * FROM efficiency_events ORDER BY sequence").fetchall()
        self.assertEqual([row[:-1] for row in after], events)
        self.assertTrue(all(row[-1] is None for row in after))
        self.assertTrue(migrated.validate_checkpoint(migrated.checkpoint()))

    def test_malformed_checkpoint_rejected(self):
        good = self.ledger.checkpoint()
        bad = [None, [], {}, dict(good, extra=True)]
        for key, values in {
            "schema": [None, CHECKPOINT_SCHEMA + ".other"],
            "instance_id": [None, 1, "x" * 32, "0" * 31, "A" * 32],
            "event_sequence": [True, False, -1, 1.0, "0", MAX_INTEGER + 1],
            "event_sha256": [False, "0" * 64],
        }.items():
            for value in values:
                bad.append(dict(good, **{key: value}))
        bad.extend(dict(good, event_sequence=1, event_sha256=value)
                   for value in [None, 5, "x" * 64, "0" * 63, "A" * 64])
        for checkpoint in bad:
            with self.subTest(checkpoint=checkpoint), self.assertRaises(LedgerError):
                self.ledger.validate_checkpoint(checkpoint)

    def test_input_checkpoint_is_not_mutated(self):
        checkpoint = self.ledger.checkpoint()
        expected = copy.deepcopy(checkpoint)
        self.ledger.validate_checkpoint(checkpoint)
        self.assertEqual(checkpoint, expected)

    def test_malformed_existing_instance_is_not_repaired_silently(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE efficiency_meta SET instance_id='invalid'")
        for operation in (lambda: ResourceLedger(self.path), self.ledger.checkpoint):
            with self.assertRaisesRegex(LedgerError, "instance identity"):
                operation()

    def test_malformed_instance_column_is_not_accepted(self):
        path = self.root / "bad-meta.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE efficiency_meta (singleton INTEGER PRIMARY KEY, schema TEXT NOT NULL, instance_id INTEGER)")
            db.execute("INSERT INTO efficiency_meta VALUES(1,'keel.efficiency.ledger.v1',5)")
        with self.assertRaisesRegex(LedgerError, "instance column"):
            ResourceLedger(path)

    def test_malformed_event_column_is_not_accepted(self):
        path = self.root / "bad-events.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE efficiency_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                event TEXT NOT NULL, payload_json TEXT NOT NULL, created_ns INTEGER NOT NULL,
                event_id INTEGER)""")
        with self.assertRaisesRegex(LedgerError, "event identity column"):
            ResourceLedger(path)

    def test_malformed_event_identity_rejected(self):
        self.ledger.reserve("request", "window", {"calls": 1})
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE efficiency_events SET event_id='invalid'")
        with self.assertRaisesRegex(LedgerError, "event identity"):
            self.ledger.checkpoint()

    def test_checkpoint_queries_do_not_write_or_request_writer_lock(self):
        self.ledger.reserve("request", "window", {"calls": 1})
        statements = []
        connections = []
        original_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            connections.append(connection)
            self.assertTrue(kwargs.get("uri"))
            self.assertTrue(args[0].endswith("?mode=ro"))
            return connection

        # WAL readers can observe the previous committed snapshot while a
        # writer has an open transaction; BEGIN IMMEDIATE would block here.
        with original_connect(self.path, isolation_level=None) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                with patch("keel_efficiency.ledger.sqlite3.connect", side_effect=tracked_connect):
                    checkpoint = self.ledger.checkpoint()
                    self.assertTrue(self.ledger.validate_checkpoint(checkpoint))
            finally:
                writer.rollback()
        self.assertEqual(len(connections), 2)
        self.assertFalse(any("IMMEDIATE" in sql for sql in statements))
        self.assertFalse(any(sql.lstrip().split()[0].upper() in
                             {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "REPLACE"}
                             for sql in statements))
        selections = [sql for sql in statements if sql.startswith("SELECT")]
        self.assertEqual(len(selections), 4)
        self.assertTrue(any("ORDER BY sequence DESC LIMIT 1" in sql for sql in selections))
        self.assertTrue(any("WHERE sequence=1" in sql for sql in selections))


if __name__ == "__main__":
    unittest.main()
