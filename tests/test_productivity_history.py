"""Indexed history migration, retained receipts and bounded-read regressions."""
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import keel_paths
import log_event
import pipeline_service as pipeline
import productivity_history as history_module
import productivity_service as service
import queue_io
from keel_efficiency.ledger import ResourceLedger
from safe_io import atomic_json, canonical, digest, read_json


class ProductivityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='keel-history-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 1800000000.0
        self.folder = self.home / 'data/productivity'
        for name in pipeline.QUEUES:
            atomic_json(self.home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        atomic_json(self.home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        for context in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
                        patch.object(log_event, 'EVENTS', str(self.home / 'data/telemetry/events.jsonl')),
                        patch.object(keel_paths, 'HOME', str(self.home)),
                        patch.dict(os.environ, {'KEEL_HOME': str(self.home),
                            'JOB_PIPELINE_HTTP_COOLDOWN_DIR': str(self.home / 'hidden_files/http-cooldowns')})):
            context.start(); self.addCleanup(context.stop)
        self.ledger = ResourceLedger(self.home / 'budget.sqlite3')
        self.ledger.create_scope('shared', {'calls': 100000, 'compute_ms': 10**9})

    def cycle(self, run_id):
        return service.run_once(self.home, run_id=run_id, ledger=self.ledger, scope_id='shared',
                                live=True, clock=lambda: self.now)

    def history(self, writable=False):
        return history_module.History(self.folder, self.home, service._validate_row, writable=writable)

    def make_legacy(self, count=3):
        for index in range(count):
            self.cycle('legacy-' + str(index))
        history = self.history()
        state = history.state(self.now)
        page = history.page()
        legacy = {key: {record['run_id']: record['row'] for record in page['rows']} if key == 'runs' else value
                  for key, value in state.items() if not key.startswith('_')}
        (self.folder / 'history.sqlite3').unlink()
        (self.folder / 'history.json').unlink()
        atomic_json(self.folder / 'journal.json', legacy)
        return legacy, (self.folder / 'journal.json').read_bytes()

    def test_legacy_status_is_readonly_then_live_migrates_all_receipts(self):
        legacy, original = self.make_legacy()
        before = self.ledger.snapshot('shared')['used']
        observed = service.status(self.home, clock=lambda: self.now)
        self.assertEqual(observed['metrics']['retained_runs'], 3)
        self.assertFalse((self.folder / 'history.sqlite3').exists())
        old = service.get_run(self.home, 'legacy-0', ledger=self.ledger, scope_id='shared', clock=lambda: self.now)
        self.assertEqual(old['receipt_sha256'], legacy['runs']['legacy-0']['receipt_sha256'])
        replay = self.cycle('legacy-0')
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.ledger.snapshot('shared')['used'], before)
        self.assertEqual((self.folder / 'journal.v1.json').read_bytes(), original)
        tombstone = read_json(self.folder / 'journal.json')
        self.assertEqual(tombstone['schema'], 'keel.productivity.history-reference.v1')
        with self.assertRaises(ValueError):
            service._load_legacy(self.folder / 'journal.json', self.now)
        self.cycle('new')
        self.assertEqual(self.history().counts()['runs'], 4)

    def test_readonly_status_and_receipts_never_write_sqlite(self):
        self.cycle('one')
        files = list(self.folder.iterdir())
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        service.status(self.home, ledger=self.ledger, scope_id='shared', clock=lambda: self.now)
        service.get_run(self.home, 'one', ledger=self.ledger, scope_id='shared', clock=lambda: self.now)
        service.receipts(self.home, ledger=self.ledger, scope_id='shared', clock=lambda: self.now)
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.folder.iterdir()})

    def test_interrupted_import_leaves_old_journal_for_retry(self):
        legacy, original = self.make_legacy()
        original_write = history_module.History._write_row
        count = 0
        def fail_after_first(instance, db, run_id, row):
            nonlocal count
            original_write(instance, db, run_id, row)
            count += 1
            if count == 1:
                raise OSError('fixture import interrupted')
        with patch.object(history_module.History, '_write_row', fail_after_first):
            with self.assertRaises(OSError):
                self.cycle('new')
        self.assertFalse((self.folder / 'history.sqlite3').exists())
        self.assertEqual((self.folder / 'journal.json').read_bytes(), original)
        self.cycle('new')
        self.assertEqual(self.history().counts()['runs'], 4)
        for run_id, row in legacy['runs'].items():
            self.assertEqual(service.get_run(self.home, run_id, clock=lambda: self.now)['receipt_sha256'], row['receipt_sha256'])

    def test_complete_bootstrap_without_manifest_finishes_only_live(self):
        self.make_legacy()
        with patch.object(history_module, 'atomic_json', side_effect=OSError('fixture manifest not published')):
            with self.assertRaises(OSError):
                self.cycle('new')
        self.assertTrue((self.folder / 'history.sqlite3').exists())
        self.assertFalse((self.folder / 'history.json').exists())
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            service.status(self.home, clock=lambda: self.now)
        self.cycle('new')
        self.assertEqual(self.history().counts()['runs'], 4)

    def test_manifest_published_before_bootstrap_finalization_can_resume(self):
        self.make_legacy()
        with patch.object(history_module.History, 'finalize_bootstrap', side_effect=OSError('fixture finalization interrupted')):
            with self.assertRaises(OSError):
                self.cycle('new')
        self.assertTrue((self.folder / 'history.json').exists())
        before = (self.folder / 'history.sqlite3').read_bytes()
        observed = service.status(self.home, clock=lambda: self.now)
        self.assertEqual(observed['metrics']['retained_runs'], 3)
        self.assertEqual((self.folder / 'history.sqlite3').read_bytes(), before)
        self.cycle('new')
        self.assertEqual(self.history().counts()['runs'], 4)

    def test_tombstone_failure_blocks_dispatch_and_next_live_finishes(self):
        _, original = self.make_legacy()
        before = self.ledger.snapshot('shared')['used']
        atomic = service.atomic_json
        def fail_tombstone(path, value):
            if value.get('schema') == 'keel.productivity.history-reference.v1':
                raise OSError('fixture fence unavailable')
            atomic(path, value)
        with patch.object(service, 'atomic_json', side_effect=fail_tombstone):
            with self.assertRaises(OSError):
                self.cycle('new')
        self.assertEqual(self.ledger.snapshot('shared')['used'], before)
        self.assertEqual((self.folder / 'journal.json').read_bytes(), original)
        self.cycle('new')
        self.assertEqual(self.history().counts()['runs'], 4)

    def test_missing_database_cannot_reimport_frozen_legacy(self):
        self.make_legacy()
        self.cycle('new')
        (self.folder / 'history.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError, 'database_missing'):
            self.cycle('reset-attempt')
        self.assertTrue((self.folder / 'journal.v1.json').exists())

    def test_changed_legacy_before_fencing_is_not_discarded(self):
        legacy, _ = self.make_legacy()
        with patch.object(service, 'atomic_json', side_effect=OSError('fixture tombstone failure')):
            with self.assertRaises(OSError):
                self.cycle('new')
        legacy['last_now'] += 1
        atomic_json(self.folder / 'journal.json', legacy)
        self.now += 1
        with self.assertRaisesRegex(ValueError, 'legacy_changed'):
            self.cycle('new')

    def test_indexed_pagination_and_replay_beyond_old_capacity(self):
        for index in range(270):
            self.cycle('run-' + str(index))
        before = self.ledger.snapshot('shared')['used']
        replay = self.cycle('run-0')
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.ledger.snapshot('shared')['used'], before)
        found, cursor = [], 0
        while True:
            page = service.receipts(self.home, after_sequence=cursor, limit=37, clock=lambda: self.now)
            self.assertLessEqual(len(page['receipts']), 37)
            found.extend(row['run_id'] for row in page['receipts'])
            cursor = page['next_sequence']
            if not page['has_more']:
                break
        self.assertEqual(found, ['run-' + str(index) for index in range(270)])
        self.assertEqual(page['total_runs'], 270)
        state = service._load(self.folder / 'journal.json', self.now)
        self.assertEqual(len(state['runs'].cache), 0)
        state['runs'].get('run-0')
        self.assertEqual(len(state['runs'].cache), 1)
        with sqlite3.connect(self.folder / 'history.sqlite3') as db:
            explanation = str(db.execute('EXPLAIN QUERY PLAN SELECT * FROM runs WHERE run_id=?', ('run-0',)).fetchall())
        self.assertIn('INDEX', explanation)

    def test_storage_refuses_receipt_rewrite_and_phase_regression(self):
        self.cycle('one')
        state = service._load(self.folder / 'journal.json', self.now, write=True)
        row = state['runs'].get('one')
        row['phase'] = 'RECORDED'
        with self.assertRaisesRegex(ValueError, 'phase_regressed'):
            service._save(self.folder / 'journal.json', state, self.now)
        state = service._load(self.folder / 'journal.json', self.now, write=True)
        row = state['runs'].get('one')
        row['result']['usage']['calls'] = 1
        row['receipt_sha256'] = digest(row['result'])
        with self.assertRaisesRegex(ValueError, 'receipt_changed'):
            service._save(self.folder / 'journal.json', state, self.now)

    def test_old_ledger_checkpoint_rejects_rollback_without_history_scan(self):
        self.cycle('one')
        backup = self.home / 'backup.sqlite3'
        with sqlite3.connect(self.ledger.path) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        self.cycle('two')
        with sqlite3.connect(backup) as source, sqlite3.connect(self.ledger.path) as destination:
            source.backup(destination)
        with self.assertRaises(ValueError):
            self.cycle('three')

    def test_selected_receipt_and_ledger_checks_are_bounded(self):
        for index in range(20):
            self.cycle('row-' + str(index))
        original = self.ledger.request
        calls = []
        def counted(request_id):
            calls.append(request_id)
            return original(request_id)
        with patch.object(self.ledger, 'request', side_effect=counted):
            self.cycle('next')
            self.assertEqual(len(calls), 0)
            self.cycle('row-0')
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
