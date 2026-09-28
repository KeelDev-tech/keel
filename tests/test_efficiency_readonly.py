"""Inspection must not initialize, migrate or acquire a writer transaction."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from keel_efficiency.defaults import configured_budget
from keel_efficiency.ledger import LedgerError, ResourceLedger


class LedgerReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "resources.sqlite3"
        self.ledger = ResourceLedger(self.path)
        self.ledger.create_scope("parent", {"calls": 200})
        self.ledger.create_scope("window", {"calls": 100}, parent_id="parent")
        self.ledger.reserve("request", "window", {"calls": 3})

    def alter(self, statement):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(statement)
            db.commit()

    def test_reads_and_checkpoints_match_writable_ledger(self):
        reader = ResourceLedger.open_readonly(self.path)
        self.assertIsInstance(reader, ResourceLedger)
        self.assertEqual(reader.snapshot(), self.ledger.snapshot())
        self.assertEqual(reader.snapshot("window"), self.ledger.snapshot("window"))
        self.assertEqual(reader.snapshot("parent")["available"]["calls"], 197)
        self.assertEqual(reader.request("request"), self.ledger.request("request"))
        checkpoint = self.ledger.checkpoint()
        self.assertEqual(reader.checkpoint(), checkpoint)
        self.assertTrue(reader.validate_checkpoint(checkpoint))

    def test_missing_ledger_does_not_create_file_or_parent(self):
        missing = self.root / "missing" / "nested" / "ledger.sqlite3"
        with self.assertRaisesRegex(LedgerError, "existing resource ledger is unavailable"):
            ResourceLedger.open_readonly(missing)
        self.assertFalse(missing.parent.parent.exists())

    def test_path_validation_precedes_sqlite_open(self):
        with patch("keel_efficiency.ledger.sqlite3.connect") as connect:
            for value in (None, "", b"ledger", ":memory:", "file:ledger?mode=rw", "bad\x00path", self.root):
                with self.subTest(path_type=type(value).__name__):
                    with self.assertRaises(LedgerError):
                        ResourceLedger.open_readonly(value)
            connect.assert_not_called()

    def test_legacy_meta_is_not_migrated(self):
        self.alter("ALTER TABLE efficiency_meta DROP COLUMN instance_id")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(LedgerError, "migration required"):
            ResourceLedger.open_readonly(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn("instance_id", [row[1] for row in db.execute("PRAGMA table_info(efficiency_meta)")])

    def test_legacy_events_are_not_migrated(self):
        self.alter("ALTER TABLE efficiency_events DROP COLUMN event_id")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(LedgerError, "migration required"):
            ResourceLedger.open_readonly(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_or_incomplete_database_is_not_initialized(self):
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with self.assertRaisesRegex(LedgerError, "schema is incomplete"):
            ResourceLedger.open_readonly(empty)
        self.assertEqual(empty.read_bytes(), b"")
        self.alter("DROP TABLE efficiency_requests")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(LedgerError, "schema is incomplete"):
            ResourceLedger.open_readonly(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_incompatible_schema_and_instance_are_rejected(self):
        self.alter("ALTER TABLE efficiency_requests DROP COLUMN metadata_json")
        with self.assertRaisesRegex(LedgerError, "schema is incompatible"):
            ResourceLedger.open_readonly(self.path)
        other = self.root / "invalid-instance.sqlite3"
        ResourceLedger(other)
        with closing(sqlite3.connect(other)) as db:
            db.execute("UPDATE efficiency_meta SET instance_id='invalid'")
            db.commit()
        with self.assertRaisesRegex(LedgerError, "instance identity"):
            ResourceLedger.open_readonly(other)

    def test_all_public_mutations_are_refused_including_idempotent_replays(self):
        reader = ResourceLedger.open_readonly(self.path)
        before = self.ledger.snapshot()
        checkpoint = self.ledger.checkpoint()
        operations = (
            lambda: reader.create_scope("window", {"calls": 100}, parent_id="parent"),
            lambda: reader.create_scope("new", {"calls": 1}),
            lambda: reader.reserve("request", "window", {"calls": 3}),
            lambda: reader.reserve("new", "window", {"calls": 1}),
            lambda: reader.mark_dispatched("request"),
            lambda: reader.settle("request", {"calls": 1}),
            lambda: reader.reconcile("request", {"calls": 1}),
            lambda: reader.mark_unknown("request", "uncertain"),
            lambda: reader.cancel("request"),
        )
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                with self.assertRaisesRegex(LedgerError, "read-only ledger cannot mutate state"):
                    operation()
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.ledger.checkpoint(), checkpoint)

    def test_reads_succeed_under_active_writer_and_ignore_uncommitted_changes(self):
        checkpoint = self.ledger.checkpoint()
        connect = sqlite3.connect

        def short_timeout(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return connect(*args, **kwargs)

        with closing(connect(self.path, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE efficiency_scopes SET locked=1 WHERE scope_id='window'")
            with patch("keel_efficiency.ledger.sqlite3.connect", side_effect=short_timeout):
                reader = ResourceLedger.open_readonly(self.path)
                for ledger in (self.ledger, reader):
                    self.assertFalse(ledger.snapshot("window")["locked"])
                    self.assertEqual(ledger.snapshot()["event_count"], 1)
                    self.assertEqual(ledger.request("request")["state"], "RESERVED")
                    self.assertEqual(ledger.checkpoint(), checkpoint)
                    self.assertTrue(ledger.validate_checkpoint(checkpoint))
            writer.rollback()

    def test_reads_do_not_recreate_deleted_database(self):
        reader = ResourceLedger.open_readonly(self.path)
        checkpoint = reader.checkpoint()
        self.path.unlink()
        for ledger in (self.ledger, reader):
            for operation in (ledger.snapshot, lambda: ledger.snapshot("window"),
                              lambda: ledger.request("request"), ledger.checkpoint,
                              lambda: ledger.validate_checkpoint(checkpoint)):
                with self.assertRaises(LedgerError):
                    operation()
                self.assertFalse(self.path.exists())

    def test_configured_budget_readonly_preserves_existing_scope_requirement(self):
        reader, scope = configured_budget(self.path, "window", readonly=True)
        self.assertEqual(scope, "window")
        self.assertEqual(reader.snapshot(scope)["available"]["calls"], 97)
        with self.assertRaisesRegex(LedgerError, "read-only ledger"):
            reader.cancel("request")
        with self.assertRaisesRegex(LedgerError, "scope does not exist"):
            configured_budget(self.path, "missing", readonly=True)
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(LedgerError):
            configured_budget(missing, "window", readonly=True)
        self.assertFalse(missing.exists())
        self.assertEqual(configured_budget(None, None, readonly=True), (None, None))
        for path, scope in ((None, "window"), (self.path, None), ("", "window"), (self.path, "")):
            with self.assertRaises(ValueError):
                configured_budget(path, scope, readonly=True)

    def test_configured_budget_default_remains_writable(self):
        ledger, scope = configured_budget(self.path, "window")
        ledger.cancel("request")
        self.assertEqual(ledger.snapshot(scope)["available"]["calls"], 100)


if __name__ == "__main__":
    unittest.main()
